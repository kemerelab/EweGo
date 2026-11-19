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
 * and pushes it into a RAM ring; the writer thread (Firmware/common/ewego-rec.c)
 * does the accounting, the CSV and the live file. The kernel keeps just 64
 * reports (0.6 s at 100 Hz) per open hidraw node, and with a single thread a
 * CSV write that blocked on the SD card while the cameras were streaming to
 * it lost up to 150 samples at a time (seen 2026-11-12). Now a slow write
 * only grows the ring.
 *
 * --live DIR writes <name>.json there (meant for tmpfs, /run/ewego) ten
 * times a second: counters plus the most recent samples, for the web
 * console's live view. --preview streams the module without writing any
 * recording (live file only).
 *
 * Exit: 0 clean and nothing lost; 2 lost or flagged samples or ring
 * overruns; 3 no reports for --stall seconds; 1 usage/device error.
 *
 * Build: make (compiles ../common/ewego-rec.c in)
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <limits.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "../common/ewego-rec.h"

#define VERSION "0.3.1"
#define USB_VID 0x1209
#define USB_PID 0x0001

#define RPT_LEN 64
#define RPT_FLAG_I2C_ERROR 0x01
#define RPT_FLAG_NO_SENSOR 0x02
#define RPT_FLAG_DROPPED   0x04

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

struct sample {
	int64_t host_mono, host_real;
	uint8_t rpt[RPT_LEN];
};

/* writer-thread accounting, plus reader counters (atomic) */
struct usb {
	uint64_t lost, gaps, flagged, i2c_err, dropped;
	uint16_t last_seq;
	int have_seq;
	uint32_t last_dev;
	int64_t dev_total;          /* accumulated device time, handles 32-bit wrap */
	int64_t first_dev, first_host, last_host;
	double dt_min, dt_max, dt_sum;
	uint64_t dt_n;
	uint32_t i2c_us_max;
	uint64_t bad_reports;       /* reader */
};

#define A_LOAD(x) __atomic_load_n(&(x), __ATOMIC_RELAXED)
#define A_INC(x) __atomic_add_fetch(&(x), 1, __ATOMIC_RELAXED)

static double drift_ppm(const struct usb *u)
{
	if (!u->have_seq || u->last_host <= u->first_host)
		return 0;
	return ((double)u->dev_total - (double)(u->last_host - u->first_host)) / (double)(u->last_host - u->first_host) * 1e6;
}

static double rate_hz(const struct usb *u)
{
	return u->dt_n ? 1e6 / (u->dt_sum / (double)u->dt_n) : 0;
}

static int bad_data(const uint8_t *rpt) { return rpt[1] & (RPT_FLAG_I2C_ERROR | RPT_FLAG_NO_SENSOR); }

static void csv_row(FILE *csv, const void *p)
{
	const struct sample *s = p;
	const uint8_t *b = s->rpt;

	rec_bno_csv(csv, s->host_real, bad_data(b) ? NULL : b + 8);
	fprintf(csv, ",%" PRId64 ",%" PRIu32 ",%u,%u,%u\n", s->host_mono, ule32(b + 4), ule16(b + 2), ule16(b + 54), b[1]);
}

static const uint8_t *valid_burst(const void *p)
{
	const struct sample *s = p;
	return bad_data(s->rpt) ? NULL : s->rpt + 8;
}

static void live_row(FILE *f, uint64_t n, const void *p)
{
	const struct sample *s = p;

	fprintf(f, "[%" PRIu64 ",%u,%" PRIu32 ",%" PRId64, n, ule16(s->rpt + 2), ule32(s->rpt + 4), s->host_mono);
	rec_bno_live(f, valid_burst(s));
	fprintf(f, ",%u]", s->rpt[1]);
}

static void live_counters(FILE *f, struct rec *r, const struct rec_snap *q)
{
	struct usb *u = r->app;

	fprintf(f, "\"samples\":%" PRIu64 ",\"lost\":%" PRIu64 ",\"gaps\":%" PRIu64 ",\"flagged\":%" PRIu64
		   ",\"i2c_errors\":%" PRIu64 ",\"queue_overruns\":%" PRIu64
		   ",\"queue_peak\":%u,\"hz\":%.2f,\"drift_ppm\":%.0f,\"i2c_us_max\":%u,",
		r->n, u->lost, u->gaps, u->flagged, u->i2c_err, q->overruns, q->peak, rate_hz(u), drift_ppm(u), u->i2c_us_max);
}

static void stats(char *buf, size_t len, struct rec *r, const struct rec_snap *q)
{
	struct usb *u = r->app;

	snprintf(buf, len, "%s: %" PRIu64 " samples, %" PRIu64 " lost, %" PRIu64 " flagged | %.2f Hz (device clock), "
		 "interval %.0f/%.0f us, i2c max %u us, drift %+.0f ppm | queue peak %u, overruns %" PRIu64,
		 r->name, r->n, u->lost, u->flagged, rate_hz(u), u->dt_n ? u->dt_min : 0.0, u->dt_n ? u->dt_max : 0.0,
		 u->i2c_us_max, drift_ppm(u), q->peak, q->overruns);
}

static void summary(FILE *js, struct rec *r, const struct rec_snap *q)
{
	struct usb *u = r->app;

	fprintf(js, "  \"samples\": %" PRIu64 ",\n"
		    "  \"lost\": %" PRIu64 ",\n  \"gaps\": %" PRIu64 ",\n  \"flagged\": %" PRIu64 ",\n"
		    "  \"i2c_errors\": %" PRIu64 ",\n  \"host_not_polling\": %" PRIu64 ",\n"
		    "  \"bad_reports\": %" PRIu64 ",\n"
		    "  \"queue\": {\"overruns\": %" PRIu64 ", \"peak\": %u, \"size\": %d},\n"
		    "  \"realtime\": %d,\n"
		    "  \"interval_us\": {\"min\": %.0f, \"mean\": %.1f, \"max\": %.0f, \"n\": %" PRIu64 "},\n"
		    "  \"effective_hz\": %.3f,\n  \"i2c_us_max\": %u,\n  \"device_minus_host_drift_ppm\": %.1f,\n"
		    "  \"first_dev_ts_us\": %" PRId64 ",\n  \"first_host_mono_us\": %" PRId64 ",\n",
		r->n, u->lost, u->gaps, u->flagged, u->i2c_err, u->dropped, A_LOAD(u->bad_reports),
		q->overruns, q->peak, REC_RING_N, r->realtime,
		u->dt_n ? u->dt_min : 0.0, u->dt_n ? u->dt_sum / (double)u->dt_n : 0.0, u->dt_n ? u->dt_max : 0.0, u->dt_n,
		rate_hz(u), u->i2c_us_max, drift_ppm(u), u->first_dev, u->first_host);
}

static void account(struct rec *r, const void *p)
{
	struct usb *w = r->app;
	const struct sample *s = p;
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
			if (!r->quiet)
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
}

static const struct rec_ops ops = {
	.sample_size = sizeof(struct sample),
	.csv_header = "timestamp,heading_deg,roll_deg,pitch_deg,quat_w,quat_x,quat_y,quat_z,"
		      "lin_accel_x,lin_accel_y,lin_accel_z,gravity_x,gravity_y,gravity_z,"
		      "accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z,mag_x,mag_y,mag_z,temp_c,"
		      "cal_sys,cal_gyro,cal_accel,cal_mag,host_mono_us,dev_ts_us,seq,i2c_us,flags\n",
	.live_cols = "[\"n\",\"seq\",\"dev_ts_us\",\"host_mono_us\",\"heading\",\"roll\",\"pitch\","
		     "\"ax\",\"ay\",\"az\",\"gx\",\"gy\",\"gz\",\"lax\",\"lay\",\"laz\",\"flags\"]",
	.account = account,
	.csv_row = csv_row,
	.valid_burst = valid_burst,
	.live_counters = live_counters,
	.live_row = live_row,
	.stats = stats,
	.summary = summary,
};

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
	char devpath[PATH_MAX];
	static struct rec rec;
	static struct usb u;
	int fd, exit_code = 0;
	int64_t t_last_rpt, t_bad_msg = 0;

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
	/* first, so a stop during the device wait is honoured */
	rec_signals();
	if (live && rec_mkdir_p(live, 0755)) {
		perror(live);
		return 1;
	}
	if (dev) {
		snprintf(devpath, sizeof(devpath), "%s", dev);
	} else {
		/* wait up to 30 s: after a hub reset the module re-enumerates late */
		int r, tries = 0;
		while ((r = find_device(devpath, sizeof(devpath))) == -1) {
			if (rec_stop) {
				fprintf(stderr, "ewego-imu-usb: stopped while waiting for the device\n");
				return 0;
			}
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

	rec.ops = &ops;
	rec.app = &u;
	rec.program = "ewego-imu-usb " VERSION;
	rec.name = name;
	rec.device = devpath;
	rec.live_dir = live;
	rec.quiet = quiet;
	rec.stats_interval = stats_interval;
	rec.preview = preview;
	rec.realtime = realtime;

	fd = open(devpath, O_RDONLY | O_CLOEXEC);
	if (fd < 0) {
		fprintf(stderr, "open %s: %s\n", devpath, strerror(errno));
		return 1;
	}
	/* the session directory only once there is a device to record */
	if (!preview && rec_open_output(&rec, out, session_dir))
		return 1;
	if (rec_start(&rec))
		return 1;
	t_last_rpt = rec.t_start;

	if (preview)
		fprintf(stderr, "ewego-imu-usb %s: %s -> live file %s/%s.json only (preview)\n", VERSION, devpath, live, name);
	else
		fprintf(stderr, "ewego-imu-usb %s: %s -> %s/%s.csv%s%s\n", VERSION, devpath, rec.dir, name,
			live ? ", live file in " : "", live ? live : "");

	/* reader loop: poll, read, timestamp, push. Nothing else. */
	while (!rec_stop) {
		struct pollfd pfd = {.fd = fd, .events = POLLIN};
		struct sample s;
		int r = poll(&pfd, 1, 500);
		int64_t now = rec_mono_us();
		if (r < 0) {
			if (errno == EINTR)
				continue;
			perror("poll");
			exit_code = 1;
			break;
		}
		if (r > 0) {
			ssize_t len;
			if (!(pfd.revents & POLLIN)) {      /* POLLERR/POLLHUP/POLLNVAL alone: node gone */
				fprintf(stderr, "poll: device error (revents 0x%x, unplugged?)\n", pfd.revents);
				exit_code = 3;
				break;
			}
			len = read(fd, s.rpt, sizeof(s.rpt));
			if (len < 0) {
				if (errno == EINTR || errno == EAGAIN)
					continue;
				fprintf(stderr, "read: %s (device unplugged?)\n", strerror(errno));
				exit_code = 3;
				break;
			}
			if (len == 0) {
				fprintf(stderr, "read: end of file (device gone)\n");
				exit_code = 3;
				break;
			}
			s.host_mono = now;
			s.host_real = rec_real_us();
			if (len == RPT_LEN && s.rpt[0] == 1) {
				t_last_rpt = now;           /* liveness: valid reports only */
				rec_push(&rec, &s);
			} else {
				/* a module streaming reports we cannot use must still trip
				 * --stall; message rate-limited (100 Hz on the RT thread) */
				uint64_t nbad = A_INC(u.bad_reports);
				if (!quiet && (nbad <= 3 || now - t_bad_msg >= 10000000)) {
					fprintf(stderr, "unexpected report #%" PRIu64 ": %zd bytes, version %u\n", nbad, len, s.rpt[0]);
					t_bad_msg = now;
				}
			}
		}
		if (now - t_last_rpt > (int64_t)stall * 1000000) {
			fprintf(stderr, "no valid reports for %d s: device stalled or unplugged\n", stall);
			exit_code = 3;
			break;
		}
		if (seconds > 0 && now - rec.t_start >= (int64_t)seconds * 1000000)
			break;
	}

	close(fd);
	{
		int oc = rec_end(&rec);
		struct rec_snap q = rec_snapshot(&rec);
		if (oc)
			exit_code = oc;              /* the recording itself is incomplete: trumps everything */
		else if (exit_code == 0 && (u.lost || u.flagged || q.overruns))
			exit_code = 2;
		rec_write_summary(&rec, exit_code);
		fprintf(stderr, "%s: %" PRIu64 " samples, %" PRIu64 " lost in %" PRIu64 " gap(s), %" PRIu64 " flagged, "
			"queue peak %u, %" PRIu64 " overruns -> exit %d\n",
			name, rec.n, u.lost, u.gaps, u.flagged, q.peak, q.overruns, exit_code);
	}
	return exit_code;
}
