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
 * RAM ring (Firmware/common/ewego-rec.c) does the accounting, the CSV, and
 * the live file for the console.
 *
 *   <name>.csv            same 28 columns as log_imu_data.py, then
 *                         host_mono_us (arrival), tick_mono_us (the scheduled
 *                         sample time), seq, xfer_us (UART round trip), flags
 *                         (bit0 = read failed, sensor columns empty;
 *                         bit1 = all-zero quaternion, the sensor is not fusing)
 *   <name>_summary.json   samples, read errors, late ticks, interval and
 *                         jitter statistics, queue overruns, fusion stalls,
 *                         write errors, exit code; rewritten every stats
 *                         interval with "in_progress": true
 *
 * Self-check: a BNO055 that browns out comes back in CONFIG mode and keeps
 * answering with zeros; 20 all-zero quaternions in a row (or 3 s of
 * identical data) make the reader check OPR_MODE and re-initialise it.
 * The port is held with TIOCEXCL: other openers get EBUSY.
 *
 * --live DIR writes DIR/<name>.json ten times a second (counters + recent
 * samples); --preview streams into the live file only.
 *
 * Exit: 0 every tick produced a sample; 2 read errors, ring overruns or a
 * fusion stall; 3 no sample for --stall seconds (sensor or UART gone);
 * 4 the recording could not be written; 1 usage/device error (EBUSY: the
 * port is held by another process).
 *
 * Build: make (compiles ../common/ewego-rec.c in)
 */
#define _GNU_SOURCE
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
#include <sys/ioctl.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#include "../common/ewego-rec.h"

#define VERSION "0.3.0"

#define BNO_CHIP_ID   0xA0
#define REG_CHIP_ID   0x00
#define REG_PAGE_ID   0x07
#define REG_ACC_X_LSB 0x08
#define REG_OPR_MODE  0x3D
#define REG_PWR_MODE  0x3E
#define MODE_CONFIG   0x00
#define MODE_NDOF     0x0C
#define BURST_LEN     REC_BNO_BURST_LEN          /* 0x08 .. 0x35 */

#define FLAG_READ_ERROR     0x01  /* read failed; sensor columns empty */
#define FLAG_FUSION_INVALID 0x02  /* read fine but the quaternion is all zero: not fusing */

#define ZERO_RUN_MAX 20           /* consecutive all-zero quaternions before checking OPR_MODE */
#define FROZEN_S 3                /* seconds of identical data before checking OPR_MODE */

#define A_LOAD(x) __atomic_load_n(&(x), __ATOMIC_RELAXED)
#define A_INC(x) __atomic_add_fetch(&(x), 1, __ATOMIC_RELAXED)

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
		fprintf(stderr, "open %s: %s%s\n", port, strerror(errno),
			errno == EBUSY ? " (another process holds the port exclusively: a recorder or preview?)" : "");
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
	/* exclusive: nobody else (the console's test scripts, a second
	 * recorder) can open the port while we own it; they get EBUSY.
	 * Processes with CAP_SYS_ADMIN are exempt (kernel rule). */
	if (ioctl(fd, TIOCEXCL) && errno != ENOTTY)
		fprintf(stderr, "note: TIOCEXCL on %s failed (%s)\n", port, strerror(errno));
	return fd;
}

/* read exactly n bytes, giving up at the deadline; returns bytes read */
static int read_exact(int fd, uint8_t *buf, int n, int64_t deadline)
{
	int got = 0;
	while (got < n) {
		int64_t left = deadline - rec_mono_us();
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

/* write n bytes by the deadline (the fd is non-blocking: wait for POLLOUT,
 * never spin on EAGAIN, which on the SCHED_FIFO thread would starve the
 * writer); -1 on error or timeout */
static int write_all(int fd, const uint8_t *buf, int n, int64_t deadline)
{
	while (n > 0) {
		ssize_t w = write(fd, buf, (size_t)n);
		if (w < 0) {
			struct pollfd p = {.fd = fd, .events = POLLOUT};
			int64_t left = deadline - rec_mono_us();
			if (errno == EINTR)
				continue;
			if (errno != EAGAIN || left <= 0)
				return -1;
			if (poll(&p, 1, (int)((left + 999) / 1000)) < 0 && errno != EINTR)
				return -1;
			continue;
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
	int64_t deadline = rec_mono_us() + (int64_t)timeout_ms * 1000;

	if (write_all(fd, cmd, 4, deadline))
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
	int64_t deadline = rec_mono_us() + 200000;

	if (write_all(fd, cmd, 5, deadline))
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
	int64_t end = rec_mono_us() + (int64_t)max_ms * 1000;
	uint8_t junk[128];

	while (rec_mono_us() < end) {
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

	for (i = 0; i < attempts && !rec_stop; i++) {
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

/* quaternion w,x,y,z = burst[24..31] */
static int quat_zero(const uint8_t *burst)
{
	static const uint8_t z[8];
	return !memcmp(burst + 24, z, sizeof(z));
}

/* ---- samples and writer callbacks ---------------------------------------- */

struct sample {
	int64_t host_mono, host_real, tick_mono;
	uint32_t seq, xfer_us;
	uint8_t flags;
	uint8_t burst[BURST_LEN];
};

struct uart {
	int baud, rate;
	/* writer thread */
	uint64_t valid, read_errors, error_runs, fusion_invalid;
	int in_error_run;
	int64_t first_host, last_valid_host;
	double dt_min, dt_max, dt_sum;
	uint64_t dt_n;
	uint32_t xfer_us_max;
	int64_t jitter_us_max;
	/* reader thread (atomic) */
	uint64_t late_ticks, err_by_code[16], err_total, reinits;
	uint64_t fusion_stalls;     /* zero-quaternion or frozen-data runs that reached the threshold */
	uint64_t fusion_reinits;    /* re-inits those runs caused (also counted in reinits) */
	uint64_t overruns_retried;  /* EE 07 (bus overrun) answers whose one retry succeeded */
};

static double rate_hz(const struct uart *u)
{
	return u->dt_n ? 1e6 / (u->dt_sum / (double)u->dt_n) : 0;
}

static const uint8_t *valid_burst(const void *p)
{
	const struct sample *s = p;
	return (s->flags & FLAG_READ_ERROR) ? NULL : s->burst;
}

static void csv_row(FILE *csv, const void *p)
{
	const struct sample *s = p;

	rec_bno_csv(csv, s->host_real, valid_burst(s));
	fprintf(csv, ",%" PRId64 ",%" PRId64 ",%" PRIu32 ",%" PRIu32 ",%u\n", s->host_mono, s->tick_mono, s->seq,
		s->xfer_us, s->flags);
}

static void live_row(FILE *f, uint64_t n, const void *p)
{
	const struct sample *s = p;

	fprintf(f, "[%" PRIu64 ",%" PRIu32 ",%" PRId64 ",%" PRId64, n, s->seq, s->tick_mono, s->host_mono);
	rec_bno_live(f, valid_burst(s));
	fprintf(f, ",%u]", s->flags);
}

static void live_counters(FILE *f, struct rec *r, const struct rec_snap *q)
{
	struct uart *u = r->app;

	/* same keys as ewego-imu-usb where they mean the same thing, so the
	 * console shows both IMUs with one piece of code */
	fprintf(f, "\"samples\":%" PRIu64 ",\"lost\":%" PRIu64 ",\"gaps\":%" PRIu64
		   ",\"flagged\":%" PRIu64 ",\"read_errors\":%" PRIu64 ",\"i2c_errors\":0,\"queue_overruns\":%" PRIu64
		   ",\"queue_peak\":%u,\"hz\":%.2f,\"drift_ppm\":0,\"xfer_us_max\":%u,\"jitter_us_max\":%" PRId64 ","
		   "\"fusion_invalid\":%" PRIu64 ",\"fusion_stalls\":%" PRIu64 ",\"reinits\":%" PRIu64 ","
		   "\"overruns_retried\":%" PRIu64 ",",
		u->valid, u->read_errors, u->error_runs, u->read_errors + u->fusion_invalid, u->read_errors,
		q->overruns, q->peak, rate_hz(u), u->xfer_us_max, u->jitter_us_max,
		u->fusion_invalid, A_LOAD(u->fusion_stalls), A_LOAD(u->reinits), A_LOAD(u->overruns_retried));
}

static void stats(char *buf, size_t len, struct rec *r, const struct rec_snap *q)
{
	struct uart *u = r->app;

	snprintf(buf, len, "%s: %" PRIu64 " samples, %" PRIu64 " read errors | %.2f Hz, interval %.0f/%.0f us, "
		 "xfer max %u us, jitter max %" PRId64 " us | queue peak %u, overruns %" PRIu64 "%s",
		 r->name, u->valid, u->read_errors, rate_hz(u), u->dt_n ? u->dt_min : 0.0, u->dt_n ? u->dt_max : 0.0,
		 u->xfer_us_max, u->jitter_us_max, q->peak, q->overruns,
		 A_LOAD(u->fusion_stalls) ? " | FUSION STALLED (sensor reset?)" : "");
}

static void summary(FILE *js, struct rec *r, const struct rec_snap *q)
{
	struct uart *u = r->app;
	uint64_t e0 = A_LOAD(u->err_by_code[0]), e7 = A_LOAD(u->err_by_code[7]), e10 = A_LOAD(u->err_by_code[10]);

	fprintf(js, "  \"baud\": %d,\n"
		    "  \"rate_hz\": %d,\n  \"samples\": %" PRIu64 ",\n"
		    "  \"lost\": %" PRIu64 ",\n  \"gaps\": %" PRIu64 ",\n  \"flagged\": %" PRIu64 ",\n"
		    "  \"read_errors\": %" PRIu64 ",\n  \"error_runs\": %" PRIu64 ",\n  \"late_ticks\": %" PRIu64 ",\n"
		    "  \"errors_by_code\": {\"timeout_or_garbage\": %" PRIu64 ", \"bus_overrun_07\": %" PRIu64
		    ", \"receive_timeout_0a\": %" PRIu64 ", \"other\": %" PRIu64 "},\n  \"reinits\": %" PRIu64 ",\n"
		    "  \"fusion_invalid\": %" PRIu64 ",\n  \"fusion_stalls\": %" PRIu64 ",\n  \"fusion_reinits\": %" PRIu64 ",\n"
		    "  \"overruns_retried\": %" PRIu64 ",\n"
		    "  \"queue\": {\"overruns\": %" PRIu64 ", \"peak\": %u, \"size\": %d},\n"
		    "  \"realtime\": %d,\n"
		    "  \"interval_us\": {\"min\": %.0f, \"mean\": %.1f, \"max\": %.0f, \"n\": %" PRIu64 "},\n"
		    "  \"effective_hz\": %.3f,\n  \"xfer_us_max\": %u,\n  \"jitter_us_max\": %" PRId64 ",\n"
		    "  \"first_host_mono_us\": %" PRId64 ",\n",
		u->baud, u->rate, u->valid, u->read_errors, u->error_runs, u->read_errors + u->fusion_invalid,
		u->read_errors, u->error_runs, A_LOAD(u->late_ticks),
		e0, e7, e10, A_LOAD(u->err_total) - e0 - e7 - e10, A_LOAD(u->reinits),
		u->fusion_invalid, A_LOAD(u->fusion_stalls), A_LOAD(u->fusion_reinits), A_LOAD(u->overruns_retried),
		q->overruns, q->peak, REC_RING_N, r->realtime,
		u->dt_n ? u->dt_min : 0.0, u->dt_n ? u->dt_sum / (double)u->dt_n : 0.0, u->dt_n ? u->dt_max : 0.0, u->dt_n,
		rate_hz(u), u->xfer_us_max, u->jitter_us_max, u->first_host);
}

static void account(struct rec *r, const void *p)
{
	struct uart *w = r->app;
	const struct sample *s = p;
	int64_t jitter = s->host_mono - s->tick_mono;

	if (r->n == 0)
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
	if (s->flags & FLAG_FUSION_INVALID)
		w->fusion_invalid++;
	if (s->xfer_us > w->xfer_us_max)
		w->xfer_us_max = s->xfer_us;
	if (jitter > w->jitter_us_max)
		w->jitter_us_max = jitter;
}

static const struct rec_ops ops = {
	.sample_size = sizeof(struct sample),
	.csv_header = "timestamp,heading_deg,roll_deg,pitch_deg,quat_w,quat_x,quat_y,quat_z,"
		      "lin_accel_x,lin_accel_y,lin_accel_z,gravity_x,gravity_y,gravity_z,"
		      "accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z,mag_x,mag_y,mag_z,temp_c,"
		      "cal_sys,cal_gyro,cal_accel,cal_mag,host_mono_us,tick_mono_us,seq,xfer_us,flags\n",
	.live_cols = "[\"n\",\"seq\",\"tick_mono_us\",\"host_mono_us\",\"heading\",\"roll\",\"pitch\","
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
	static struct rec rec;
	static struct uart u;
	int fd, exit_code = 0, r;
	int64_t t_last_ok, period_us, next_tick;
	int64_t t_last_reinit = 0, t_last_check = 0;
	uint32_t seq = 0;
	uint64_t zero_run = 0, same_run = 0;
	uint8_t prev_burst[BURST_LEN];
	int have_prev = 0, frozen_noted = 0;

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

	u.baud = baud;
	u.rate = rate;
	rec.ops = &ops;
	rec.app = &u;
	rec.program = "ewego-imu-uart " VERSION;
	rec.name = name;
	rec.device = port;
	rec.live_dir = live;
	rec.quiet = quiet;
	rec.stats_interval = stats_interval;
	rec.preview = preview;
	rec.realtime = realtime;

	if (live && rec_mkdir_p(live, 0755)) {
		perror(live);
		return 1;
	}
	rec_signals();

	fd = uart_open(port, baud);
	if (fd < 0)
		return 1;
	r = bno_init(fd, quiet, 20);
	if (r) {
		fprintf(stderr, "ewego-imu-uart: %s: %s\n", port,
			r == -1 ? "no BNO055 answered (chip id)" : "could not configure the sensor");
		return 1;
	}

	if (!preview && rec_open_output(&rec, out, session_dir))
		return 1;
	if (rec_start(&rec))
		return 1;
	t_last_ok = rec.t_start;

	if (preview)
		fprintf(stderr, "ewego-imu-uart %s: %s @ %d, %d Hz -> live file %s/%s.json only (preview)\n",
			VERSION, port, baud, rate, live, name);
	else
		fprintf(stderr, "ewego-imu-uart %s: %s @ %d, %d Hz -> %s/%s.csv%s%s\n", VERSION, port, baud, rate,
			rec.dir, name, live ? ", live file in " : "", live ? live : "");

	/* reader loop: one burst read per tick on a steady schedule */
	period_us = 1000000 / rate;
	next_tick = rec.t_start + period_us;
	while (!rec_stop) {
		struct sample s;
		struct timespec ts = {.tv_sec = next_tick / 1000000, .tv_nsec = (next_tick % 1000000) * 1000};
		int64_t now, t0;

		while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL) == EINTR && !rec_stop)
			;
		if (rec_stop)
			break;
		memset(&s, 0, sizeof(s));
		s.tick_mono = next_tick;
		s.seq = seq++;
		t0 = rec_mono_us();
		/* 30 ms: the 48-byte answer takes 4.2 ms on the wire plus whatever
		 * the sensor adds; a tick that runs long is resynchronised below */
		r = bno_read(fd, REG_ACC_X_LSB, BURST_LEN, s.burst, 30);
		if (r == 0x07) {     /* bus overrun: the sensor was mid-update; give it 2 ms and ask once more */
			drain_quiet(fd, 2, 10);
			r = bno_read(fd, REG_ACC_X_LSB, BURST_LEN, s.burst, 30);
			if (r == 0)
				A_INC(u.overruns_retried);
		}
		now = rec_mono_us();
		s.host_mono = now;
		s.host_real = rec_real_us();
		s.xfer_us = (uint32_t)(now - t0);
		if (r) {
			int code = (r > 0 && r < 16) ? r : 0;
			uint64_t nerr;
			s.flags |= FLAG_READ_ERROR;
			A_INC(u.err_by_code[code]);
			nerr = A_INC(u.err_total);
			if (!quiet && (nerr <= 10 || nerr % 1000 == 0))
				fprintf(stderr, "read error #%" PRIu64 " at %.1f s: %s (0x%02X)\n", nerr,
					(double)(now - rec.t_start) / 1e6, ERR_NAMES[code], r > 0 ? r : 0);
			drain_quiet(fd, 5, 60);
			/* two seconds without a sample: the sensor may have reset (it
			 * comes back in config mode, silent on the fusion registers) */
			if (now - t_last_ok > 2000000 && now - t_last_reinit > 2000000) {
				t_last_reinit = now;
				fprintf(stderr, "no valid sample for 2 s: re-initialising the sensor (attempt %" PRIu64 ")\n",
					A_INC(u.reinits));
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
			/* self-check: a BNO055 that browned out and reset (POR 0.4-0.65 s)
			 * comes back in CONFIG mode and answers burst reads normally,
			 * with the data registers at zero. In NDOF the quaternion is a
			 * unit vector, never all zero. */
			if (quat_zero(s.burst)) {
				s.flags |= FLAG_FUSION_INVALID;
				if (++zero_run == ZERO_RUN_MAX)
					A_INC(u.fusion_stalls);
			} else {
				zero_run = 0;
			}
			/* frozen: identical sensor data (acc/mag/gyr/fusion; LSB noise
			 * makes that impossible on a live sensor) for FROZEN_S seconds */
			if (have_prev && !memcmp(s.burst, prev_burst, 44)) {
				if (++same_run == (uint64_t)rate * FROZEN_S)
					A_INC(u.fusion_stalls);
			} else {
				same_run = 0;
			}
			memcpy(prev_burst, s.burst, sizeof(prev_burst));
			have_prev = 1;
		}

		rec_push(&rec, &s);

		if (!r && (zero_run >= ZERO_RUN_MAX || same_run >= (uint64_t)rate * FROZEN_S) &&
		    now - t_last_check >= 2000000) {
			int zero = zero_run >= ZERO_RUN_MAX;
			uint8_t mode = 0xFF;
			int mr = bno_read(fd, REG_OPR_MODE, 1, &mode, 30);
			t_last_check = now;
			if (mr) {
				fprintf(stderr, "%s: reading OPR_MODE failed (%d)\n",
					zero ? "all-zero quaternion" : "data frozen", mr);
				drain_quiet(fd, 5, 60);
			} else if ((mode & 0x0F) != MODE_NDOF || zero) {
				fprintf(stderr, "%s at %.1f s, OPR_MODE 0x%02X%s: re-initialising the sensor (attempt %" PRIu64 ")\n",
					zero ? "all-zero quaternion for 20 samples" : "sensor data frozen for 3 s",
					(double)(now - rec.t_start) / 1e6, mode,
					(mode & 0x0F) != MODE_NDOF ? " (not NDOF: the sensor reset)" : "", A_INC(u.reinits));
				A_INC(u.fusion_reinits);
				if (bno_init(fd, quiet, 3) == 0)
					fprintf(stderr, "sensor re-initialised\n");
				zero_run = same_run = 0;
				have_prev = 0;
			} else if (!frozen_noted) {
				fprintf(stderr, "sensor data frozen for 3 s but OPR_MODE is NDOF: not re-initialising\n");
				frozen_noted = 1;
			}
		}
		if (same_run == 0)
			frozen_noted = 0;

		next_tick += period_us;
		now = rec_mono_us();                /* a re-init above may have taken a while */
		if (next_tick < now) {              /* fell behind by a whole period: resynchronise */
			A_INC(u.late_ticks);
			next_tick = now + period_us;
		}
		if (seconds > 0 && now - rec.t_start >= (int64_t)seconds * 1000000)
			break;
	}

	/* leave the sensor in config mode, like the Python logger did */
	bno_write(fd, REG_OPR_MODE, MODE_CONFIG);
	ioctl(fd, TIOCNXCL);
	close(fd);
	{
		int oc = rec_end(&rec);
		struct rec_snap q = rec_snapshot(&rec);
		uint64_t e0 = u.err_by_code[0], e7 = u.err_by_code[7], e10 = u.err_by_code[10];
		if (oc)
			exit_code = oc;              /* the recording itself is incomplete: trumps everything */
		else if (exit_code == 0 && (u.read_errors || q.overruns || u.fusion_stalls))
			exit_code = 2;
		rec_write_summary(&rec, exit_code);
		fprintf(stderr, "%s: %" PRIu64 " samples, %" PRIu64 " read errors in %" PRIu64 " run(s) (timeout/garbage %" PRIu64
			", bus overrun %" PRIu64 ", receive timeout %" PRIu64 "), %" PRIu64 " re-inits, %" PRIu64
			" late ticks, %" PRIu64 " fusion stalls (%" PRIu64 " zero-quaternion samples), queue peak %u, %" PRIu64
			" overruns -> exit %d\n",
			name, u.valid, u.read_errors, u.error_runs, e0, e7, e10, u.reinits, u.late_ticks, u.fusion_stalls,
			u.fusion_invalid, q.peak, q.overruns, exit_code);
	}
	return exit_code;
}
