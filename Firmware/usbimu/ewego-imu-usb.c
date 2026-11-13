/*
 * ewego-imu-usb — log the hub_imu USB IMU (STM32F042 + BNO055, HID).
 *
 * Reads 64-byte HID input reports from /dev/hidrawN (see
 * Hardware/hub_imu/firmware/src/report.h), stamps each with CLOCK_MONOTONIC and
 * CLOCK_REALTIME on arrival, and writes:
 *
 *   <name>.csv            same columns as Firmware/IMU/log_imu_data.py
 *                         (timestamp, euler, quat, lin acc, gravity, acc, gyro,
 *                         mag, temp, calib) plus host_mono_us, dev_ts_us, seq,
 *                         i2c_us, flags
 *   <name>_summary.json   samples, lost (sequence gaps), flagged samples,
 *                         device-clock interval statistics, host-vs-device
 *                         clock drift, queue overruns, exit code
 *
 * Two threads, like ewego-cam: the reader (the main thread, optionally
 * SCHED_FIFO with --rt) only polls the hidraw node, timestamps each report
 * and pushes it into a RAM ring; the writer thread does the accounting, the
 * CSV and the live file. The kernel keeps just 64 reports (0.6 s at 100 Hz)
 * per open hidraw node, and with a single thread a CSV write that blocked on
 * the SD card while the cameras were streaming to it lost up to 150 samples
 * at a time (seen 2026-11-12). Now a slow write only grows the ring.
 *
 * --live DIR writes <name>.json there (meant for tmpfs, /run/ewego) ten
 * times a second: counters plus the most recent samples, for the web
 * console's live view. --preview streams the module without writing any
 * recording (live file only).
 *
 * Exit: 0 clean and nothing lost; 2 lost or flagged samples or ring
 * overruns; 3 no reports for --stall seconds; 1 usage/device error.
 *
 * Build: gcc -O2 -Wall -Wextra -pthread -static -o ewego-imu-usb ewego-imu-usb.c
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <limits.h>
#include <poll.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define VERSION "0.3"
#define USB_VID 0x1209
#define USB_PID 0x0001

#define RPT_LEN 64
#define RPT_FLAG_I2C_ERROR 0x01
#define RPT_FLAG_NO_SENSOR 0x02
#define RPT_FLAG_DROPPED   0x04

#define RING_N 8192      /* samples between reader and writer: ~80 s at 100 Hz */
#define LIVE_N 64        /* most recent samples kept for the live file */
#define LIVE_INTERVAL_US 100000

static volatile sig_atomic_t g_stop;
static void on_signal(int s) { (void)s; g_stop = 1; }

static int64_t mono_us(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (int64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}

static int64_t real_us(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_REALTIME, &ts);
	return (int64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}

static int16_t le16(const uint8_t *p) { return (int16_t)(p[0] | (p[1] << 8)); }
static uint16_t ule16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static uint32_t ule32(const uint8_t *p) { return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24); }

/* Find the hidraw node whose HID_ID names our VID:PID. */
static int find_device(char *out, size_t outlen)
{
	DIR *d = opendir("/sys/class/hidraw");
	struct dirent *e;
	int found = 0;
	char want[64];
	snprintf(want, sizeof(want), "HID_ID=0003:%08X:%08X", USB_VID, USB_PID);
	if (!d)
		return -1;
	while ((e = readdir(d))) {
		char path[PATH_MAX], line[256];
		FILE *f;
		if (strncmp(e->d_name, "hidraw", 6))
			continue;
		snprintf(path, sizeof(path), "/sys/class/hidraw/%s/device/uevent", e->d_name);
		f = fopen(path, "r");
		if (!f)
			continue;
		while (fgets(line, sizeof(line), f)) {
			line[strcspn(line, "\n")] = 0;
			if (!strcmp(line, want)) {
				snprintf(out, outlen, "/dev/%s", e->d_name);
				found++;
			}
		}
		fclose(f);
	}
	closedir(d);
	return found == 1 ? 0 : (found ? -2 : -1);
}

/* ---- reader -> writer ring ---------------------------------------------- */

struct sample {
	int64_t host_mono, host_real;
	uint8_t rpt[RPT_LEN];
};

struct ring {
	pthread_mutex_t m;
	pthread_cond_t cv;
	struct sample s[RING_N];
	unsigned head, tail, count, peak;
	uint64_t overruns;
	int done;
};

/* ---- writer state ------------------------------------------------------- */

struct writer {
	struct ring *r;
	FILE *csv;                 /* NULL in --preview */
	const char *name, *devpath, *live_dir, *session;
	int quiet, stats_interval, preview;
	int64_t t_start;
	/* accounting */
	uint64_t n, lost, gaps, flagged, i2c_err, dropped;
	uint16_t last_seq;
	int have_seq;
	uint32_t last_dev;
	int64_t dev_total;          /* accumulated device time, handles 32-bit wrap */
	int64_t first_dev, first_host, last_host;
	double dt_min, dt_max, dt_sum;
	uint64_t dt_n;
	uint32_t i2c_us_max;
	/* live file */
	struct sample live[LIVE_N];
	uint64_t live_n[LIVE_N];    /* running sample index of each live entry */
	unsigned live_head, live_count;
	pthread_t thread;
};

static double drift_ppm(const struct writer *w)
{
	if (!w->have_seq || w->last_host <= w->first_host)
		return 0;
	return ((double)w->dev_total - (double)(w->last_host - w->first_host)) / (double)(w->last_host - w->first_host) * 1e6;
}

static double rate_hz(const struct writer *w)
{
	return w->dt_n ? 1e6 / (w->dt_sum / (double)w->dt_n) : 0;
}

static void csv_row(FILE *csv, const struct sample *s)
{
	const uint8_t *b = s->rpt;
	uint8_t flags = b[1];
	uint16_t seq = ule16(b + 2);
	uint32_t dev = ule32(b + 4);
	const uint8_t *bu = b + 8;
	uint16_t i2c_us = ule16(b + 54);
	time_t secs = (time_t)(s->host_real / 1000000);
	struct tm tm;
	char ts[40];

	localtime_r(&secs, &tm);
	strftime(ts, sizeof(ts), "%Y-%m-%dT%H:%M:%S", &tm);
	fprintf(csv, "%s.%06" PRId64, ts, s->host_real % 1000000);
	if (flags & (RPT_FLAG_I2C_ERROR | RPT_FLAG_NO_SENSOR)) {
		for (int i = 0; i < 27; i++)
			fputs(",", csv);
	} else {
		fprintf(csv, ",%.4f,%.4f,%.4f", le16(bu + 18) / 16.0, le16(bu + 20) / 16.0, le16(bu + 22) / 16.0);
		fprintf(csv, ",%.6f,%.6f,%.6f,%.6f", le16(bu + 24) / 16384.0, le16(bu + 26) / 16384.0,
			le16(bu + 28) / 16384.0, le16(bu + 30) / 16384.0);
		fprintf(csv, ",%.2f,%.2f,%.2f", le16(bu + 32) / 100.0, le16(bu + 34) / 100.0, le16(bu + 36) / 100.0);
		fprintf(csv, ",%.2f,%.2f,%.2f", le16(bu + 38) / 100.0, le16(bu + 40) / 100.0, le16(bu + 42) / 100.0);
		fprintf(csv, ",%.2f,%.2f,%.2f", le16(bu + 0) / 100.0, le16(bu + 2) / 100.0, le16(bu + 4) / 100.0);
		fprintf(csv, ",%.4f,%.4f,%.4f", le16(bu + 12) / 16.0, le16(bu + 14) / 16.0, le16(bu + 16) / 16.0);
		fprintf(csv, ",%.4f,%.4f,%.4f", le16(bu + 6) / 16.0, le16(bu + 8) / 16.0, le16(bu + 10) / 16.0);
		fprintf(csv, ",%d,%u,%u,%u,%u", (int8_t)bu[44], (bu[45] >> 6) & 3, (bu[45] >> 4) & 3,
			(bu[45] >> 2) & 3, bu[45] & 3);
	}
	fprintf(csv, ",%" PRId64 ",%" PRIu32 ",%u,%u,%u\n", s->host_mono, dev, seq, i2c_us, flags);
}

/* The live file: counters and the last LIVE_N samples, written atomically. */
static void write_live(struct writer *w, int64_t now)
{
	char tmp[PATH_MAX + 64], fin[PATH_MAX + 64];
	FILE *f;
	unsigned i;
	const uint8_t *latest = NULL;

	snprintf(fin, sizeof(fin), "%s/%s.json", w->live_dir, w->name);
	snprintf(tmp, sizeof(tmp), "%s/.%s.json.tmp", w->live_dir, w->name);
	f = fopen(tmp, "w");
	if (!f)
		return;
	/* latest sample with valid sensor data, for temperature and calibration */
	for (i = 0; i < w->live_count; i++) {
		const struct sample *s = &w->live[(w->live_head + LIVE_N - 1 - i) % LIVE_N];
		if (!(s->rpt[1] & (RPT_FLAG_I2C_ERROR | RPT_FLAG_NO_SENSOR))) {
			latest = s->rpt + 8;
			break;
		}
	}
	fprintf(f, "{\"program\":\"ewego-imu-usb %s\",\"name\":\"%s\",\"device\":\"%s\",\"preview\":%d,",
		VERSION, w->name, w->devpath, w->preview);
	if (w->session)
		fprintf(f, "\"session\":\"%s\",", w->session);
	else
		fputs("\"session\":null,", f);
	fprintf(f, "\"elapsed_s\":%.1f,\"samples\":%" PRIu64 ",\"lost\":%" PRIu64 ",\"gaps\":%" PRIu64
		   ",\"flagged\":%" PRIu64 ",\"i2c_errors\":%" PRIu64 ",\"queue_overruns\":%" PRIu64
		   ",\"queue_peak\":%u,\"hz\":%.2f,\"drift_ppm\":%.0f,\"i2c_us_max\":%u,",
		(double)(now - w->t_start) / 1e6, w->n, w->lost, w->gaps, w->flagged, w->i2c_err,
		w->r->overruns, w->r->peak, rate_hz(w), drift_ppm(w), w->i2c_us_max);
	if (latest)
		fprintf(f, "\"temp_c\":%d,\"cal\":[%u,%u,%u,%u],", (int8_t)latest[44], (latest[45] >> 6) & 3,
			(latest[45] >> 4) & 3, (latest[45] >> 2) & 3, latest[45] & 3);
	else
		fputs("\"temp_c\":null,\"cal\":null,", f);
	fprintf(f, "\"updated_mono_us\":%" PRId64 ",\"updated_real_us\":%" PRId64 ",", now, real_us());
	fputs("\"cols\":[\"n\",\"seq\",\"dev_ts_us\",\"host_mono_us\",\"heading\",\"roll\",\"pitch\","
	      "\"ax\",\"ay\",\"az\",\"gx\",\"gy\",\"gz\",\"lax\",\"lay\",\"laz\",\"flags\"],\"recent\":[", f);
	for (i = 0; i < w->live_count; i++) {
		unsigned k = (w->live_head + LIVE_N - w->live_count + i) % LIVE_N;
		const struct sample *s = &w->live[k];
		const uint8_t *bu = s->rpt + 8;
		uint8_t flags = s->rpt[1];
		fprintf(f, "%s[%" PRIu64 ",%u,%" PRIu32 ",%" PRId64, i ? "," : "", w->live_n[k],
			ule16(s->rpt + 2), ule32(s->rpt + 4), s->host_mono);
		if (flags & (RPT_FLAG_I2C_ERROR | RPT_FLAG_NO_SENSOR))
			fputs(",null,null,null,null,null,null,null,null,null,null,null,null", f);
		else
			fprintf(f, ",%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.3f,%.3f,%.3f,%.2f,%.2f,%.2f",
				le16(bu + 18) / 16.0, le16(bu + 20) / 16.0, le16(bu + 22) / 16.0,
				le16(bu + 0) / 100.0, le16(bu + 2) / 100.0, le16(bu + 4) / 100.0,
				le16(bu + 12) / 16.0, le16(bu + 14) / 16.0, le16(bu + 16) / 16.0,
				le16(bu + 32) / 100.0, le16(bu + 34) / 100.0, le16(bu + 36) / 100.0);
		fprintf(f, ",%u]", flags);
	}
	fputs("]}\n", f);
	fclose(f);
	rename(tmp, fin);
}

static void process_sample(struct writer *w, const struct sample *s)
{
	const uint8_t *b = s->rpt;
	uint8_t flags = b[1];
	uint16_t seq = ule16(b + 2);
	uint32_t dev = ule32(b + 4);
	uint16_t i2c_us = ule16(b + 54);

	if (!w->have_seq) {
		w->have_seq = 1;
		w->first_dev = dev;
		w->first_host = s->host_mono;
		w->dev_total = 0;
	} else {
		uint16_t expect = (uint16_t)(w->last_seq + 1);
		uint32_t ddt = dev - w->last_dev;      /* unsigned wrap-safe */
		if (seq != expect) {
			uint16_t missing = (uint16_t)(seq - expect);
			w->gaps++;
			w->lost += missing;
			if (!w->quiet)
				fprintf(stderr, "gap: seq %u -> %u (%u lost)\n", w->last_seq, seq, missing);
		}
		w->dev_total += ddt;
		if (!(flags & RPT_FLAG_DROPPED) && seq == expect) {
			double d = ddt;
			if (w->dt_n == 0 || d < w->dt_min) w->dt_min = d;
			if (d > w->dt_max) w->dt_max = d;
			w->dt_sum += d;
			w->dt_n++;
		}
	}
	w->last_seq = seq;
	w->last_dev = dev;
	w->last_host = s->host_mono;
	if (flags) {
		w->flagged++;
		if (flags & RPT_FLAG_I2C_ERROR) w->i2c_err++;
		if (flags & RPT_FLAG_DROPPED) w->dropped++;
	}
	if (i2c_us > w->i2c_us_max)
		w->i2c_us_max = i2c_us;
	if (w->csv)
		csv_row(w->csv, s);
	if (w->live_dir) {
		w->live[w->live_head] = *s;
		w->live_n[w->live_head] = w->n;
		w->live_head = (w->live_head + 1) % LIVE_N;
		if (w->live_count < LIVE_N)
			w->live_count++;
	}
	w->n++;
}

static void print_stats(struct writer *w, int64_t now)
{
	uint64_t ovr;
	unsigned peak;

	pthread_mutex_lock(&w->r->m);
	ovr = w->r->overruns;
	peak = w->r->peak;
	pthread_mutex_unlock(&w->r->m);
	printf("[%7.1fs] %s: %" PRIu64 " samples, %" PRIu64 " lost, %" PRIu64 " flagged | %.2f Hz (device clock), "
	       "interval %.0f/%.0f us, i2c max %u us, drift %+.0f ppm | queue peak %u, overruns %" PRIu64 "\n",
	       (double)(now - w->t_start) / 1e6, w->name, w->n, w->lost, w->flagged, rate_hz(w),
	       w->dt_n ? w->dt_min : 0.0, w->dt_n ? w->dt_max : 0.0, w->i2c_us_max, drift_ppm(w), peak, ovr);
	fflush(stdout);
}

static void *writer_main(void *arg)
{
	struct writer *w = arg;
	struct ring *r = w->r;
	int64_t t_last_stats = w->t_start, t_last_flush = w->t_start, t_last_live = w->t_start;

	for (;;) {
		struct sample s;
		int got = 0, done;
		int64_t now;

		pthread_mutex_lock(&r->m);
		if (r->count == 0 && !r->done) {
			struct timespec ts;
			clock_gettime(CLOCK_MONOTONIC, &ts);
			ts.tv_nsec += 100000000;
			if (ts.tv_nsec >= 1000000000) {
				ts.tv_sec++;
				ts.tv_nsec -= 1000000000;
			}
			pthread_cond_timedwait(&r->cv, &r->m, &ts);
		}
		if (r->count) {
			s = r->s[r->tail];
			r->tail = (r->tail + 1) % RING_N;
			r->count--;
			got = 1;
		}
		done = r->done;
		pthread_mutex_unlock(&r->m);

		if (got)
			process_sample(w, &s);
		else if (done)
			break;

		now = mono_us();
		if (w->stats_interval > 0 && !w->quiet && now - t_last_stats >= (int64_t)w->stats_interval * 1000000) {
			print_stats(w, now);
			t_last_stats = now;
		}
		if (w->csv && now - t_last_flush >= 1000000) {
			fflush(w->csv);
			t_last_flush = now;
		}
		if (w->live_dir && now - t_last_live >= LIVE_INTERVAL_US) {
			write_live(w, now);
			t_last_live = now;
		}
	}
	return NULL;
}

static void usage(FILE *f)
{
	fprintf(f, "ewego-imu-usb %s — log the hub_imu USB IMU\n"
		   "usage: ewego-imu-usb --out DIR [--device /dev/hidrawN] [--name imu] [--seconds N]\n"
		   "       [--stats N] [--stall N] [--no-session-dir] [--rt] [--live DIR] [--quiet] [--list]\n"
		   "       ewego-imu-usb --preview --live DIR     stream to the live file only, record nothing\n"
		   "  --device   default: the single hidraw node with VID:PID %04x:%04x\n"
		   "  --rt       SCHED_FIFO priority 10 and mlockall for the reader thread (needs root)\n"
		   "  --live DIR write DIR/<name>.json (counters + recent samples) ten times a second\n",
		VERSION, USB_VID, USB_PID);
}

int main(int argc, char **argv)
{
	const char *dev = NULL, *out = NULL, *name = "imu", *live = NULL;
	int seconds = 0, stats_interval = 5, stall = 5, session_dir = 1, quiet = 0, realtime = 0, preview = 0;
	static const struct option lo[] = {
		{"device", 1, 0, 'd'}, {"out", 1, 0, 'o'}, {"name", 1, 0, 'n'}, {"seconds", 1, 0, 't'},
		{"stats", 1, 0, 'S'}, {"stall", 1, 0, 'x'}, {"no-session-dir", 0, 0, 'N'},
		{"rt", 0, 0, 'r'}, {"live", 1, 0, 'L'}, {"preview", 0, 0, 'P'},
		{"quiet", 0, 0, 'q'}, {"list", 0, 0, 'l'}, {"help", 0, 0, 'h'}, {0, 0, 0, 0},
	};
	int opt;
	char devpath[PATH_MAX], dir[PATH_MAX], path[PATH_MAX + 64];
	static struct ring ring;    /* static: RING_N samples is too big for the stack */
	static struct writer w;
	FILE *csv = NULL;
	int fd, exit_code = 0;
	uint64_t bad_reports = 0;
	int64_t t_start, t_last_rpt, real0;

	while ((opt = getopt_long(argc, argv, "d:o:n:t:S:x:NrL:Pqlh", lo, NULL)) != -1) {
		switch (opt) {
		case 'd': dev = optarg; break;
		case 'o': out = optarg; break;
		case 'n': name = optarg; break;
		case 't': seconds = atoi(optarg); break;
		case 'S': stats_interval = atoi(optarg); break;
		case 'x': stall = atoi(optarg); break;
		case 'N': session_dir = 0; break;
		case 'r': realtime = 1; break;
		case 'L': live = optarg; break;
		case 'P': preview = 1; break;
		case 'q': quiet = 1; break;
		case 'l': {
			int r = find_device(devpath, sizeof(devpath));
			printf("%s\n", r == 0 ? devpath : r == -2 ? "more than one hub_imu device" : "no hub_imu device");
			return r == 0 ? 0 : 1;
		}
		case 'h': usage(stdout); return 0;
		default: usage(stderr); return 1;
		}
	}
	if ((!out && !preview) || (preview && !live)) {
		usage(stderr);
		return 1;
	}
	if (dev) {
		snprintf(devpath, sizeof(devpath), "%s", dev);
	} else {
		/* wait up to 30 s: after a hub reset the module re-enumerates late */
		int r, tries = 0;
		while ((r = find_device(devpath, sizeof(devpath))) == -1) {
			if (++tries >= 60) {
				fprintf(stderr, "ewego-imu-usb: no hub_imu device after 30 s (plugged in, firmware >= v0.3.0?)\n");
				return 1;
			}
			if (tries == 1)
				fprintf(stderr, "ewego-imu-usb: waiting up to 30 s for the hub_imu device ...\n");
			usleep(500000);
		}
		if (r == -2) {
			fprintf(stderr, "ewego-imu-usb: more than one hub_imu; use --device\n");
			return 1;
		}
	}

	if (!preview) {
		if (session_dir) {
			time_t t = time(NULL);
			struct tm tm;
			char stamp[32];
			localtime_r(&t, &tm);
			strftime(stamp, sizeof(stamp), "%Y%m%d_%H%M%S", &tm);
			snprintf(dir, sizeof(dir), "%s/%s", out, stamp);
		} else {
			snprintf(dir, sizeof(dir), "%s", out);
		}
		if ((mkdir(out, 0755) && errno != EEXIST) || (mkdir(dir, 0755) && errno != EEXIST)) {
			perror(dir);
			return 1;
		}
	}
	if (live && mkdir(live, 0755) && errno != EEXIST) {
		perror(live);
		return 1;
	}

	fd = open(devpath, O_RDONLY | O_CLOEXEC);
	if (fd < 0) {
		fprintf(stderr, "open %s: %s\n", devpath, strerror(errno));
		return 1;
	}

	if (!preview) {
		snprintf(path, sizeof(path), "%s/%s.csv", dir, name);
		csv = fopen(path, "w");
		if (!csv) {
			perror(path);
			return 1;
		}
		setvbuf(csv, NULL, _IOFBF, 64 * 1024);
		fputs("timestamp,heading_deg,roll_deg,pitch_deg,quat_w,quat_x,quat_y,quat_z,"
		      "lin_accel_x,lin_accel_y,lin_accel_z,gravity_x,gravity_y,gravity_z,"
		      "accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z,mag_x,mag_y,mag_z,temp_c,"
		      "cal_sys,cal_gyro,cal_accel,cal_mag,host_mono_us,dev_ts_us,seq,i2c_us,flags\n", csv);
	}

	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);

	t_start = t_last_rpt = mono_us();
	real0 = real_us();

	pthread_mutex_init(&ring.m, NULL);
	{
		/* the writer's timed wait uses CLOCK_MONOTONIC deadlines */
		pthread_condattr_t ca;
		pthread_condattr_init(&ca);
		pthread_condattr_setclock(&ca, CLOCK_MONOTONIC);
		pthread_cond_init(&ring.cv, &ca);
		pthread_condattr_destroy(&ca);
	}
	w.r = &ring;
	w.csv = csv;
	w.name = name;
	w.devpath = devpath;
	w.live_dir = live;
	w.session = preview ? NULL : dir;
	w.quiet = quiet;
	w.stats_interval = stats_interval;
	w.preview = preview;
	w.t_start = t_start;
	if (pthread_create(&w.thread, NULL, writer_main, &w)) {
		perror("pthread_create");
		return 1;
	}

	/* after the writer exists, so it keeps the normal scheduling class */
	if (realtime) {
		struct sched_param sp = {.sched_priority = 10};
		if (sched_setscheduler(0, SCHED_FIFO, &sp))
			fprintf(stderr, "note: SCHED_FIFO not granted (%s)\n", strerror(errno));
		if (mlockall(MCL_CURRENT | MCL_FUTURE))
			fprintf(stderr, "note: mlockall failed (%s)\n", strerror(errno));
	}

	if (preview)
		fprintf(stderr, "ewego-imu-usb %s: %s -> live file %s/%s.json only (preview)\n", VERSION, devpath, live, name);
	else
		fprintf(stderr, "ewego-imu-usb %s: %s -> %s/%s.csv%s%s\n", VERSION, devpath, dir, name,
			live ? ", live file in " : "", live ? live : "");

	/* reader loop: poll, read, timestamp, push. Nothing else. */
	while (!g_stop) {
		struct pollfd pfd = {.fd = fd, .events = POLLIN};
		struct sample s;
		int r = poll(&pfd, 1, 500);
		int64_t now = mono_us();
		if (r < 0) {
			if (errno == EINTR)
				continue;
			perror("poll");
			exit_code = 1;
			break;
		}
		if (r == 0) {
			if (now - t_last_rpt > (int64_t)stall * 1000000) {
				fprintf(stderr, "no reports for %d s: device stalled or unplugged\n", stall);
				exit_code = 3;
				break;
			}
		} else {
			ssize_t len = read(fd, s.rpt, sizeof(s.rpt));
			if (len < 0) {
				if (errno == EINTR)
					continue;
				fprintf(stderr, "read: %s (device unplugged?)\n", strerror(errno));
				exit_code = 3;
				break;
			}
			s.host_mono = now;
			s.host_real = real_us();
			t_last_rpt = now;
			if (len != RPT_LEN || s.rpt[0] != 1) {
				bad_reports++;
				if (!quiet)
					fprintf(stderr, "unexpected report: %zd bytes, version %u\n", len, s.rpt[0]);
				continue;
			}
			pthread_mutex_lock(&ring.m);
			if (ring.count == RING_N) {
				ring.overruns++;
			} else {
				ring.s[ring.head] = s;
				ring.head = (ring.head + 1) % RING_N;
				ring.count++;
				if (ring.count > ring.peak)
					ring.peak = ring.count;
			}
			pthread_cond_signal(&ring.cv);
			pthread_mutex_unlock(&ring.m);
		}
		if (seconds > 0 && now - t_start >= (int64_t)seconds * 1000000)
			break;
	}

	close(fd);
	pthread_mutex_lock(&ring.m);
	ring.done = 1;
	pthread_cond_signal(&ring.cv);
	pthread_mutex_unlock(&ring.m);
	pthread_join(w.thread, NULL);
	if (csv)
		fclose(csv);
	if (live) {
		snprintf(path, sizeof(path), "%s/%s.json", live, name);
		unlink(path);
	}
	if (exit_code == 0 && (w.lost || w.flagged || ring.overruns))
		exit_code = 2;

	if (!preview) {
		FILE *js;
		snprintf(path, sizeof(path), "%s/%s_summary.json", dir, name);
		js = fopen(path, "w");
		if (js) {
			fprintf(js, "{\n  \"program\": \"ewego-imu-usb %s\",\n  \"device\": \"%s\",\n  \"samples\": %" PRIu64 ",\n"
				    "  \"lost\": %" PRIu64 ",\n  \"gaps\": %" PRIu64 ",\n  \"flagged\": %" PRIu64 ",\n"
				    "  \"i2c_errors\": %" PRIu64 ",\n  \"host_not_polling\": %" PRIu64 ",\n"
				    "  \"bad_reports\": %" PRIu64 ",\n"
				    "  \"queue\": {\"overruns\": %" PRIu64 ", \"peak\": %u, \"size\": %d},\n"
				    "  \"realtime\": %d,\n"
				    "  \"interval_us\": {\"min\": %.0f, \"mean\": %.1f, \"max\": %.0f, \"n\": %" PRIu64 "},\n"
				    "  \"effective_hz\": %.3f,\n  \"i2c_us_max\": %u,\n  \"device_minus_host_drift_ppm\": %.1f,\n"
				    "  \"first_dev_ts_us\": %" PRId64 ",\n  \"first_host_mono_us\": %" PRId64 ",\n"
				    "  \"realtime_minus_monotonic_us\": %" PRId64 ",\n  \"exit_code\": %d\n}\n",
				VERSION, devpath, w.n, w.lost, w.gaps, w.flagged, w.i2c_err, w.dropped, bad_reports,
				ring.overruns, ring.peak, RING_N, realtime,
				w.dt_n ? w.dt_min : 0.0, w.dt_n ? w.dt_sum / (double)w.dt_n : 0.0, w.dt_n ? w.dt_max : 0.0, w.dt_n,
				rate_hz(&w), w.i2c_us_max, drift_ppm(&w),
				w.first_dev, w.first_host, real0 - t_start, exit_code);
			fclose(js);
		}
	}
	fprintf(stderr, "%s: %" PRIu64 " samples, %" PRIu64 " lost in %" PRIu64 " gap(s), %" PRIu64 " flagged, "
		"queue peak %u, %" PRIu64 " overruns -> exit %d\n",
		name, w.n, w.lost, w.gaps, w.flagged, ring.peak, ring.overruns, exit_code);
	return exit_code;
}
