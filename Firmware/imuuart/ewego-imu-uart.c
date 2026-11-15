/*
 * ewego-imu-uart — log the wearable's own BNO055 over its UART (ttyAMA5).
 *
 * The C port of Firmware/IMU/log_imu_data.py. Talks the BNO055 UART
 * protocol (115200 8N1: 0xAA 0x01 reg len -> 0xBB len data, or 0xEE err),
 * puts the sensor in NDOF fusion mode and reads registers 0x08..0x35 (acc,
 * mag, gyr, euler, quaternion, linear acc, gravity, temperature,
 * calibration: 46 bytes) in ONE transaction per sample on a steady timer,
 * 100 Hz by default (the fusion output rate), instead of the script's nine
 * reads at 50 Hz.
 *
 * Same shape as ewego-imu-usb: the reader (main thread, --rt for SCHED_FIFO)
 * only does the UART transaction and timestamps it; a writer thread behind a
 * RAM ring does the accounting, the CSV, and the live file for the console.
 *
 *   <name>.csv            same 28 columns as log_imu_data.py, then
 *                         host_mono_us (arrival), tick_mono_us (the scheduled
 *                         sample time), seq, xfer_us (UART round trip), flags
 *                         (bit0 = read failed; sensor columns empty)
 *   <name>_summary.json   samples, read errors, late ticks, interval and
 *                         jitter statistics, queue overruns, exit code
 *
 * --live DIR writes DIR/<name>.json ten times a second (counters + recent
 * samples); --preview streams into the live file only.
 *
 * Exit: 0 every tick produced a sample; 2 read errors or ring overruns;
 * 3 no sample for --stall seconds (sensor or UART gone); 1 usage/device error.
 *
 * Build: gcc -O2 -Wall -Wextra -pthread -static -o ewego-imu-uart ewego-imu-uart.c
 */
#define _GNU_SOURCE
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
#include <termios.h>
#include <time.h>
#include <unistd.h>

#define VERSION "0.2"

#define BNO_CHIP_ID   0xA0
#define REG_CHIP_ID   0x00
#define REG_PAGE_ID   0x07
#define REG_ACC_X_LSB 0x08
#define REG_OPR_MODE  0x3D
#define REG_PWR_MODE  0x3E
#define MODE_CONFIG   0x00
#define MODE_NDOF     0x0C
#define BURST_LEN     46          /* 0x08 .. 0x35 */

#define FLAG_READ_ERROR 0x01

#define RING_N 8192
#define LIVE_N 64
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

/* ---- BNO055 UART protocol ----------------------------------------------- */

static speed_t baud_const(int baud)
{
	switch (baud) {
	case 9600: return B9600;
	case 19200: return B19200;
	case 38400: return B38400;
	case 57600: return B57600;
	case 115200: return B115200;
	default: return 0;
	}
}

static int uart_open(const char *port, int baud)
{
	struct termios t;
	speed_t sp = baud_const(baud);
	int fd;

	if (!sp) {
		fprintf(stderr, "unsupported baud %d\n", baud);
		return -1;
	}
	fd = open(port, O_RDWR | O_NOCTTY | O_NONBLOCK | O_CLOEXEC);
	if (fd < 0) {
		fprintf(stderr, "open %s: %s\n", port, strerror(errno));
		return -1;
	}
	if (tcgetattr(fd, &t)) {
		perror("tcgetattr");
		close(fd);
		return -1;
	}
	cfmakeraw(&t);
	t.c_cflag = (t.c_cflag & ~(CSIZE | PARENB | CSTOPB | CRTSCTS)) | CS8 | CREAD | CLOCAL;
	t.c_cc[VMIN] = 0;
	t.c_cc[VTIME] = 0;
	cfsetispeed(&t, sp);
	cfsetospeed(&t, sp);
	if (tcsetattr(fd, TCSANOW, &t)) {
		perror("tcsetattr");
		close(fd);
		return -1;
	}
	tcflush(fd, TCIOFLUSH);
	return fd;
}

/* read exactly n bytes, giving up at the deadline; returns bytes read */
static int read_exact(int fd, uint8_t *buf, int n, int64_t deadline)
{
	int got = 0;
	while (got < n) {
		int64_t left = deadline - mono_us();
		struct pollfd p = {.fd = fd, .events = POLLIN};
		int r;
		if (left <= 0)
			break;
		r = poll(&p, 1, (int)((left + 999) / 1000));
		if (r <= 0) {
			if (r < 0 && errno == EINTR)
				continue;
			break;
		}
		r = (int)read(fd, buf + got, (size_t)(n - got));
		if (r < 0) {
			if (errno == EAGAIN || errno == EINTR)
				continue;
			break;
		}
		got += r;
	}
	return got;
}

static int write_all(int fd, const uint8_t *buf, int n)
{
	while (n > 0) {
		ssize_t w = write(fd, buf, (size_t)n);
		if (w < 0) {
			if (errno == EINTR || errno == EAGAIN)
				continue;
			return -1;
		}
		buf += w;
		n -= (int)w;
	}
	return 0;
}

/*
 * Register read. Returns 0 and fills buf, or a positive BNO055 error code
 * (0x07 = buffer overrun is common and transient), or -1 on timeout / junk.
 */
static int bno_read(int fd, uint8_t reg, int len, uint8_t *buf, int timeout_ms)
{
	uint8_t cmd[4] = {0xAA, 0x01, reg, (uint8_t)len}, hdr[2];
	int64_t deadline = mono_us() + (int64_t)timeout_ms * 1000;

	if (write_all(fd, cmd, 4))
		return -1;
	if (read_exact(fd, hdr, 2, deadline) != 2)
		return -1;
	if (hdr[0] == 0xEE)
		return hdr[1] ? hdr[1] : -1;
	if (hdr[0] != 0xBB || hdr[1] != (uint8_t)len) {
		tcflush(fd, TCIFLUSH);
		return -1;
	}
	return read_exact(fd, buf, len, deadline) == len ? 0 : -1;
}

static int bno_write(int fd, uint8_t reg, uint8_t val)
{
	uint8_t cmd[5] = {0xAA, 0x00, reg, 1, val}, rsp[2];
	int64_t deadline = mono_us() + 200000;

	if (write_all(fd, cmd, 5))
		return -1;
	if (read_exact(fd, rsp, 2, deadline) != 2)
		return -1;
	return (rsp[0] == 0xEE && rsp[1] == 0x01) ? 0 : (rsp[1] ? rsp[1] : -1);
}

/* BNO055 UART status codes (datasheet 4.7); index 0 is our own "timeout or garbage" */
static const char *const ERR_NAMES[16] = {
	"timeout/garbage", "write ok", "read fail", "write fail", "invalid address", "write disabled",
	"wrong start byte", "bus overrun", "max length", "min length", "receive timeout",
	"0x0B", "0x0C", "0x0D", "0x0E", "0x0F",
};

/* After a failed transaction the sensor's (late) answer may still be
 * arriving; read and discard until the line is quiet for quiet_ms, so the
 * next request's reply is not glued to stale bytes. */
static void drain_quiet(int fd, int quiet_ms, int max_ms)
{
	int64_t end = mono_us() + (int64_t)max_ms * 1000;
	uint8_t junk[128];

	while (mono_us() < end) {
		struct pollfd p = {.fd = fd, .events = POLLIN};
		if (poll(&p, 1, quiet_ms) <= 0)
			return;
		if (read(fd, junk, sizeof(junk)) <= 0)
			return;
	}
}

/* chip id with retries (the BNO055 UART is slow to answer after power-up), then NDOF */
static int bno_init(int fd, int quiet, int attempts)
{
	uint8_t id = 0;
	int i, r = -1;

	for (i = 0; i < attempts && !g_stop; i++) {
		tcflush(fd, TCIOFLUSH);
		r = bno_read(fd, REG_CHIP_ID, 1, &id, 300);
		if (r == 0 && id == BNO_CHIP_ID)
			break;
		if (!quiet)
			fprintf(stderr, "ewego-imu-uart: chip id attempt %d: %s (0x%02X)\n", i + 1,
				r == 0 ? "wrong id" : r > 0 ? "bus error" : "no answer", id);
		usleep(500000);
	}
	if (r != 0 || id != BNO_CHIP_ID)
		return -1;
	if (bno_write(fd, REG_OPR_MODE, MODE_CONFIG))
		return -2;
	usleep(25000);
	if (bno_write(fd, REG_PWR_MODE, 0x00))
		return -2;
	usleep(10000);
	if (bno_write(fd, REG_PAGE_ID, 0x00))
		return -2;
	usleep(10000);
	if (bno_write(fd, REG_OPR_MODE, MODE_NDOF))
		return -2;
	usleep(50000);          /* datasheet: 19 ms config -> fusion mode */
	return 0;
}

/* ---- reader -> writer ring ---------------------------------------------- */

struct sample {
	int64_t host_mono, host_real, tick_mono;
	uint32_t seq, xfer_us;
	uint8_t flags;
	uint8_t burst[BURST_LEN];
};

struct ring {
	pthread_mutex_t m;
	pthread_cond_t cv;
	struct sample s[RING_N];
	unsigned head, tail, count, peak;
	uint64_t overruns;
	int done;
};

struct writer {
	struct ring *r;
	FILE *csv;
	const char *name, *port, *live_dir, *session;
	int quiet, stats_interval, preview, rate;
	int64_t t_start;
	uint64_t n, valid, read_errors, error_runs;
	int in_error_run;
	int64_t first_host, last_valid_host;
	double dt_min, dt_max, dt_sum;
	uint64_t dt_n;
	uint32_t xfer_us_max;
	int64_t jitter_us_max;
	struct sample live[LIVE_N];
	uint64_t live_n[LIVE_N];
	unsigned live_head, live_count;
	pthread_t thread;
};

static double rate_hz(const struct writer *w)
{
	return w->dt_n ? 1e6 / (w->dt_sum / (double)w->dt_n) : 0;
}

static void csv_row(FILE *csv, const struct sample *s)
{
	const uint8_t *bu = s->burst;
	time_t secs = (time_t)(s->host_real / 1000000);
	struct tm tm;
	char ts[40];

	localtime_r(&secs, &tm);
	strftime(ts, sizeof(ts), "%Y-%m-%dT%H:%M:%S", &tm);
	fprintf(csv, "%s.%06" PRId64, ts, s->host_real % 1000000);
	if (s->flags & FLAG_READ_ERROR) {
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
	fprintf(csv, ",%" PRId64 ",%" PRId64 ",%" PRIu32 ",%" PRIu32 ",%u\n", s->host_mono, s->tick_mono, s->seq,
		s->xfer_us, s->flags);
}

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
	for (i = 0; i < w->live_count; i++) {
		const struct sample *s = &w->live[(w->live_head + LIVE_N - 1 - i) % LIVE_N];
		if (!(s->flags & FLAG_READ_ERROR)) {
			latest = s->burst;
			break;
		}
	}
	fprintf(f, "{\"program\":\"ewego-imu-uart %s\",\"name\":\"%s\",\"device\":\"%s\",\"preview\":%d,",
		VERSION, w->name, w->port, w->preview);
	if (w->session)
		fprintf(f, "\"session\":\"%s\",", w->session);
	else
		fputs("\"session\":null,", f);
	/* same keys as ewego-imu-usb where they mean the same thing, so the
	 * console shows both IMUs with one piece of code */
	fprintf(f, "\"elapsed_s\":%.1f,\"samples\":%" PRIu64 ",\"lost\":%" PRIu64 ",\"gaps\":%" PRIu64
		   ",\"flagged\":%" PRIu64 ",\"read_errors\":%" PRIu64 ",\"i2c_errors\":0,\"queue_overruns\":%" PRIu64
		   ",\"queue_peak\":%u,\"hz\":%.2f,\"drift_ppm\":0,\"xfer_us_max\":%u,\"jitter_us_max\":%" PRId64 ",",
		(double)(now - w->t_start) / 1e6, w->valid, w->read_errors, w->error_runs, w->read_errors, w->read_errors,
		w->r->overruns, w->r->peak, rate_hz(w), w->xfer_us_max, w->jitter_us_max);
	if (latest)
		fprintf(f, "\"temp_c\":%d,\"cal\":[%u,%u,%u,%u],", (int8_t)latest[44], (latest[45] >> 6) & 3,
			(latest[45] >> 4) & 3, (latest[45] >> 2) & 3, latest[45] & 3);
	else
		fputs("\"temp_c\":null,\"cal\":null,", f);
	fprintf(f, "\"updated_mono_us\":%" PRId64 ",\"updated_real_us\":%" PRId64 ",", now, real_us());
	fputs("\"cols\":[\"n\",\"seq\",\"tick_mono_us\",\"host_mono_us\",\"heading\",\"roll\",\"pitch\","
	      "\"ax\",\"ay\",\"az\",\"gx\",\"gy\",\"gz\",\"lax\",\"lay\",\"laz\",\"flags\"],\"recent\":[", f);
	for (i = 0; i < w->live_count; i++) {
		unsigned k = (w->live_head + LIVE_N - w->live_count + i) % LIVE_N;
		const struct sample *s = &w->live[k];
		const uint8_t *bu = s->burst;
		fprintf(f, "%s[%" PRIu64 ",%" PRIu32 ",%" PRId64 ",%" PRId64, i ? "," : "", w->live_n[k],
			s->seq, s->tick_mono, s->host_mono);
		if (s->flags & FLAG_READ_ERROR)
			fputs(",null,null,null,null,null,null,null,null,null,null,null,null", f);
		else
			fprintf(f, ",%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.3f,%.3f,%.3f,%.2f,%.2f,%.2f",
				le16(bu + 18) / 16.0, le16(bu + 20) / 16.0, le16(bu + 22) / 16.0,
				le16(bu + 0) / 100.0, le16(bu + 2) / 100.0, le16(bu + 4) / 100.0,
				le16(bu + 12) / 16.0, le16(bu + 14) / 16.0, le16(bu + 16) / 16.0,
				le16(bu + 32) / 100.0, le16(bu + 34) / 100.0, le16(bu + 36) / 100.0);
		fprintf(f, ",%u]", s->flags);
	}
	fputs("]}\n", f);
	fclose(f);
	rename(tmp, fin);
}

static void process_sample(struct writer *w, const struct sample *s)
{
	int64_t jitter = s->host_mono - s->tick_mono;

	if (w->n == 0)
		w->first_host = s->host_mono;
	if (s->flags & FLAG_READ_ERROR) {
		w->read_errors++;
		if (!w->in_error_run) {
			w->error_runs++;
			w->in_error_run = 1;
		}
	} else {
		w->in_error_run = 0;
		if (w->valid && s->host_mono > w->last_valid_host) {
			double d = (double)(s->host_mono - w->last_valid_host);
			if (w->dt_n == 0 || d < w->dt_min) w->dt_min = d;
			if (d > w->dt_max) w->dt_max = d;
			w->dt_sum += d;
			w->dt_n++;
		}
		w->valid++;
		w->last_valid_host = s->host_mono;
	}
	if (s->xfer_us > w->xfer_us_max)
		w->xfer_us_max = s->xfer_us;
	if (jitter > w->jitter_us_max)
		w->jitter_us_max = jitter;
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
	printf("[%7.1fs] %s: %" PRIu64 " samples, %" PRIu64 " read errors | %.2f Hz, interval %.0f/%.0f us, "
	       "xfer max %u us, jitter max %" PRId64 " us | queue peak %u, overruns %" PRIu64 "\n",
	       (double)(now - w->t_start) / 1e6, w->name, w->valid, w->read_errors, rate_hz(w),
	       w->dt_n ? w->dt_min : 0.0, w->dt_n ? w->dt_max : 0.0, w->xfer_us_max, w->jitter_us_max, peak, ovr);
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
	fprintf(f, "ewego-imu-uart %s — log the wearable's BNO055 over UART\n"
		   "usage: ewego-imu-uart --out DIR [--port /dev/ttyAMA5] [--baud 115200] [--rate 100] [--name imu-uart]\n"
		   "       [--seconds N] [--stats N] [--stall N] [--no-session-dir] [--rt] [--live DIR] [--quiet]\n"
		   "       ewego-imu-uart --preview --live DIR     stream to the live file only, record nothing\n"
		   "  --rate N   samples per second (default 100 = the BNO055 fusion rate; 46-byte burst per sample)\n"
		   "  --rt       SCHED_FIFO priority 10 and mlockall for the reader thread (needs root)\n"
		   "  --live DIR write DIR/<name>.json (counters + recent samples) ten times a second\n",
		VERSION);
}

int main(int argc, char **argv)
{
	const char *port = "/dev/ttyAMA5", *out = NULL, *name = "imu-uart", *live = NULL;
	int baud = 115200, rate = 100, seconds = 0, stats_interval = 5, stall = 5, session_dir = 1;
	int quiet = 0, realtime = 0, preview = 0;
	static const struct option lo[] = {
		{"port", 1, 0, 'p'}, {"baud", 1, 0, 'b'}, {"rate", 1, 0, 'R'}, {"out", 1, 0, 'o'}, {"name", 1, 0, 'n'},
		{"seconds", 1, 0, 't'}, {"stats", 1, 0, 'S'}, {"stall", 1, 0, 'x'}, {"no-session-dir", 0, 0, 'N'},
		{"rt", 0, 0, 'r'}, {"live", 1, 0, 'L'}, {"preview", 0, 0, 'P'}, {"quiet", 0, 0, 'q'},
		{"help", 0, 0, 'h'}, {0, 0, 0, 0},
	};
	int opt;
	char dir[PATH_MAX], path[PATH_MAX + 64];
	static struct ring ring;
	static struct writer w;
	FILE *csv = NULL;
	int fd, exit_code = 0, r;
	int64_t t_start, t_last_ok, real0, period_us, next_tick;
	uint64_t late_ticks = 0, err_by_code[16] = {0}, err_total = 0, reinits = 0;
	int64_t t_last_reinit = 0;
	uint32_t seq = 0;

	while ((opt = getopt_long(argc, argv, "p:b:R:o:n:t:S:x:NrL:Pqh", lo, NULL)) != -1) {
		switch (opt) {
		case 'p': port = optarg; break;
		case 'b': baud = atoi(optarg); break;
		case 'R': rate = atoi(optarg); break;
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
		case 'h': usage(stdout); return 0;
		default: usage(stderr); return 1;
		}
	}
	if ((!out && !preview) || (preview && !live) || rate < 1 || rate > 200) {
		usage(stderr);
		return 1;
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

	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);

	fd = uart_open(port, baud);
	if (fd < 0)
		return 1;
	r = bno_init(fd, quiet, 20);
	if (r) {
		fprintf(stderr, "ewego-imu-uart: %s: %s\n", port,
			r == -1 ? "no BNO055 answered (chip id)" : "could not configure the sensor");
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
		      "cal_sys,cal_gyro,cal_accel,cal_mag,host_mono_us,tick_mono_us,seq,xfer_us,flags\n", csv);
	}

	t_start = t_last_ok = mono_us();
	real0 = real_us();

	pthread_mutex_init(&ring.m, NULL);
	{
		pthread_condattr_t ca;
		pthread_condattr_init(&ca);
		pthread_condattr_setclock(&ca, CLOCK_MONOTONIC);
		pthread_cond_init(&ring.cv, &ca);
		pthread_condattr_destroy(&ca);
	}
	w.r = &ring;
	w.csv = csv;
	w.name = name;
	w.port = port;
	w.live_dir = live;
	w.session = preview ? NULL : dir;
	w.quiet = quiet;
	w.stats_interval = stats_interval;
	w.preview = preview;
	w.rate = rate;
	w.t_start = t_start;
	if (pthread_create(&w.thread, NULL, writer_main, &w)) {
		perror("pthread_create");
		return 1;
	}
	if (realtime) {
		struct sched_param sp = {.sched_priority = 10};
		if (sched_setscheduler(0, SCHED_FIFO, &sp))
			fprintf(stderr, "note: SCHED_FIFO not granted (%s)\n", strerror(errno));
		if (mlockall(MCL_CURRENT | MCL_FUTURE))
			fprintf(stderr, "note: mlockall failed (%s)\n", strerror(errno));
	}

	if (preview)
		fprintf(stderr, "ewego-imu-uart %s: %s @ %d, %d Hz -> live file %s/%s.json only (preview)\n",
			VERSION, port, baud, rate, live, name);
	else
		fprintf(stderr, "ewego-imu-uart %s: %s @ %d, %d Hz -> %s/%s.csv%s%s\n", VERSION, port, baud, rate,
			dir, name, live ? ", live file in " : "", live ? live : "");

	/* reader loop: one burst read per tick on a steady schedule */
	period_us = 1000000 / rate;
	next_tick = t_start + period_us;
	while (!g_stop) {
		struct sample s;
		struct timespec ts = {.tv_sec = next_tick / 1000000, .tv_nsec = (next_tick % 1000000) * 1000};
		int64_t now, t0;

		while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL) == EINTR && !g_stop)
			;
		if (g_stop)
			break;
		memset(&s, 0, sizeof(s));
		s.tick_mono = next_tick;
		s.seq = seq++;
		t0 = mono_us();
		/* 30 ms: the 48-byte answer takes 4.2 ms on the wire plus whatever
		 * the sensor adds; a tick that runs long is resynchronised below */
		r = bno_read(fd, REG_ACC_X_LSB, BURST_LEN, s.burst, 30);
		if (r == 0x07) {     /* bus overrun: the sensor was mid-update; give it 2 ms and ask once more */
			drain_quiet(fd, 2, 10);
			r = bno_read(fd, REG_ACC_X_LSB, BURST_LEN, s.burst, 30);
		}
		now = mono_us();
		s.host_mono = now;
		s.host_real = real_us();
		s.xfer_us = (uint32_t)(now - t0);
		if (r) {
			int code = (r > 0 && r < 16) ? r : 0;
			s.flags |= FLAG_READ_ERROR;
			err_by_code[code]++;
			err_total++;
			if (!quiet && (err_total <= 10 || err_total % 1000 == 0))
				fprintf(stderr, "read error #%" PRIu64 " at %.1f s: %s (0x%02X)\n", err_total,
					(double)(now - t_start) / 1e6, ERR_NAMES[code], r > 0 ? r : 0);
			drain_quiet(fd, 5, 60);
			/* two seconds without a sample: the sensor may have reset (it
			 * comes back in config mode, silent on the fusion registers) */
			if (now - t_last_ok > 2000000 && now - t_last_reinit > 2000000) {
				t_last_reinit = now;
				reinits++;
				fprintf(stderr, "no valid sample for 2 s: re-initialising the sensor (attempt %" PRIu64 ")\n", reinits);
				if (bno_init(fd, quiet, 3) == 0)
					fprintf(stderr, "sensor re-initialised\n");
			}
			if (now - t_last_ok > (int64_t)stall * 1000000) {
				fprintf(stderr, "no valid sample for %d s: sensor or UART gone\n", stall);
				exit_code = 3;
				break;
			}
		} else {
			t_last_ok = now;
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

		next_tick += period_us;
		if (next_tick < now) {              /* fell behind by a whole period: resynchronise */
			late_ticks++;
			next_tick = now + period_us;
		}
		if (seconds > 0 && now - t_start >= (int64_t)seconds * 1000000)
			break;
	}

	/* leave the sensor in config mode, like the Python logger did */
	bno_write(fd, REG_OPR_MODE, MODE_CONFIG);
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
	if (exit_code == 0 && (w.read_errors || ring.overruns))
		exit_code = 2;

	if (!preview) {
		FILE *js;
		snprintf(path, sizeof(path), "%s/%s_summary.json", dir, name);
		js = fopen(path, "w");
		if (js) {
			fprintf(js, "{\n  \"program\": \"ewego-imu-uart %s\",\n  \"device\": \"%s\",\n  \"baud\": %d,\n"
				    "  \"rate_hz\": %d,\n  \"samples\": %" PRIu64 ",\n"
				    "  \"lost\": %" PRIu64 ",\n  \"gaps\": %" PRIu64 ",\n  \"flagged\": %" PRIu64 ",\n"
				    "  \"read_errors\": %" PRIu64 ",\n  \"error_runs\": %" PRIu64 ",\n  \"late_ticks\": %" PRIu64 ",\n"
				    "  \"errors_by_code\": {\"timeout_or_garbage\": %" PRIu64 ", \"bus_overrun_07\": %" PRIu64
				    ", \"receive_timeout_0a\": %" PRIu64 ", \"other\": %" PRIu64 "},\n  \"reinits\": %" PRIu64 ",\n"
				    "  \"queue\": {\"overruns\": %" PRIu64 ", \"peak\": %u, \"size\": %d},\n"
				    "  \"realtime\": %d,\n"
				    "  \"interval_us\": {\"min\": %.0f, \"mean\": %.1f, \"max\": %.0f, \"n\": %" PRIu64 "},\n"
				    "  \"effective_hz\": %.3f,\n  \"xfer_us_max\": %u,\n  \"jitter_us_max\": %" PRId64 ",\n"
				    "  \"first_host_mono_us\": %" PRId64 ",\n"
				    "  \"realtime_minus_monotonic_us\": %" PRId64 ",\n  \"exit_code\": %d\n}\n",
				VERSION, port, baud, rate, w.valid, w.read_errors, w.error_runs, w.read_errors,
				w.read_errors, w.error_runs, late_ticks,
				err_by_code[0], err_by_code[7], err_by_code[10],
				err_total - err_by_code[0] - err_by_code[7] - err_by_code[10], reinits,
				ring.overruns, ring.peak, RING_N, realtime,
				w.dt_n ? w.dt_min : 0.0, w.dt_n ? w.dt_sum / (double)w.dt_n : 0.0, w.dt_n ? w.dt_max : 0.0, w.dt_n,
				rate_hz(&w), w.xfer_us_max, w.jitter_us_max, w.first_host, real0 - t_start, exit_code);
			fclose(js);
		}
	}
	fprintf(stderr, "%s: %" PRIu64 " samples, %" PRIu64 " read errors in %" PRIu64 " run(s) (timeout/garbage %" PRIu64
		", bus overrun %" PRIu64 ", receive timeout %" PRIu64 "), %" PRIu64 " re-inits, %" PRIu64
		" late ticks, queue peak %u, %" PRIu64 " overruns -> exit %d\n",
		name, w.valid, w.read_errors, w.error_runs, err_by_code[0], err_by_code[7], err_by_code[10], reinits,
		late_ticks, ring.peak, ring.overruns, exit_code);
	return exit_code;
}
