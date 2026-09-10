/*
 * ewego-cam — record one UVC (USB) camera to disk with kernel timestamps.
 *
 * Talks V4L2 to the in-kernel uvcvideo driver (not libuvc). For every frame
 * the driver hands over, it writes the compressed frame, the kernel's
 * CLOCK_MONOTONIC timestamp of the frame's arrival, and an index record
 * carrying the driver's sequence number and flags, so lost frames and
 * error-flagged frames are accounted for exactly.
 *
 * Disk I/O never touches the capture thread: frames are copied into a RAM
 * queue and a writer thread drains it with steady, small writeback
 * (sync_file_range every few MB) instead of periodic fdatasync bursts,
 * because SD-card flush stalls of a few hundred ms were enough to lose USB
 * isochronous data on the CM4.
 *
 * Output, per run, in <out>/<session>/ (session = start time, or --no-session-dir):
 *   <name>.mjpeg            concatenated JPEG frames (play_with_timestamps.py reads this)
 *   <name>_timestamps.bin   int64 little-endian, microseconds, CLOCK_MONOTONIC, one per frame
 *   <name>_index.bin        32-byte records: u64 byte offset in .mjpeg, i64 timestamp us,
 *                           u32 sequence, u32 bytes, u32 v4l2 flags, u32 reserved
 *   <name>_summary.json     counts, gaps, intervals, clock offsets, written at exit
 *
 * Exit status: 0 clean and no frames lost; 2 frames lost, error-flagged, or
 * dropped by a full write queue; 3 camera stalled; 1 usage or device error.
 *
 * Build: gcc -O2 -Wall -Wextra -static -o ewego-cam ewego-cam.c
 * No dependencies beyond libc and the kernel headers.
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <glob.h>
#include <inttypes.h>
#include <limits.h>
#include <poll.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include <linux/videodev2.h>

#define VERSION "0.2"
#define MAX_BUFFERS 32

struct idx_rec {
	uint64_t offset;
	int64_t ts_us;
	uint32_t seq;
	uint32_t bytes;
	uint32_t flags;
	uint32_t reserved;
} __attribute__((packed));

struct cfg {
	const char *device;
	const char *out;
	const char *name;
	int width, height, fps;
	int buffers;
	int seconds;
	int stats_interval;
	int stall_timeout;
	size_t ring_bytes;   /* RAM queue budget between capture and writer */
	size_t wb_chunk;     /* start writeback every this many bytes */
	int session_dir;
	int realtime;
	int quiet;
};

struct stats {
	uint64_t frames, bytes, lost, gaps, errors, stalls;
	uint32_t first_seq, last_seq;
	int have_seq;
	int64_t first_ts, last_ts;
	double dt_min, dt_max, dt_sum;
	uint64_t dt_n;
	/* per stats interval */
	uint64_t i_bytes;
	double i_dt_min, i_dt_max, i_dt_sum;
	uint64_t i_dt_n;
	char gap_list[512];
};

/* ---- frame queue between capture and writer ---------------------------- */

struct frame {
	struct frame *next;
	uint8_t *data;
	size_t len;
	int64_t ts_us;
	uint32_t seq, flags;
};

struct queue {
	pthread_mutex_t m;
	pthread_cond_t cv;
	struct frame *head, *tail;
	size_t bytes, max_bytes, high_water;
	uint64_t frames, overruns;
	int done;
};

struct writer {
	struct queue *q;
	int vid_fd, ts_fd, idx_fd;
	size_t wb_chunk;
	/* results */
	uint64_t written_frames, written_bytes;
	double write_max_ms;
	int error;
};

static volatile sig_atomic_t g_stop;

static void on_signal(int sig)
{
	(void)sig;
	g_stop = 1;
}

static int xioctl(int fd, unsigned long req, void *arg)
{
	int r;
	do {
		r = ioctl(fd, req, arg);
	} while (r == -1 && errno == EINTR);
	return r;
}

static int64_t now_mono_us(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (int64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}

static int64_t now_real_us(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_REALTIME, &ts);
	return (int64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}

/* Minimal sd_notify(3) so `systemctl status` shows a live line; no libsystemd. */
static void sd_notify(const char *msg)
{
	const char *path = getenv("NOTIFY_SOCKET");
	struct sockaddr_un addr;
	int fd;
	size_t len;

	if (!path || (path[0] != '/' && path[0] != '@'))
		return;
	fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
	if (fd < 0)
		return;
	memset(&addr, 0, sizeof(addr));
	addr.sun_family = AF_UNIX;
	len = strlen(path);
	if (len >= sizeof(addr.sun_path))
		len = sizeof(addr.sun_path) - 1;
	memcpy(addr.sun_path, path, len);
	if (addr.sun_path[0] == '@')
		addr.sun_path[0] = 0;
	sendto(fd, msg, strlen(msg), MSG_NOSIGNAL, (struct sockaddr *)&addr,
	       (socklen_t)(offsetof(struct sockaddr_un, sun_path) + len));
	close(fd);
}

static int write_all(int fd, const void *buf, size_t len)
{
	const uint8_t *p = buf;
	while (len > 0) {
		ssize_t n = write(fd, p, len);
		if (n < 0) {
			if (errno == EINTR)
				continue;
			return -1;
		}
		p += n;
		len -= (size_t)n;
	}
	return 0;
}

static void *writer_main(void *arg)
{
	struct writer *w = arg;
	struct queue *q = w->q;
	uint64_t offset = 0;
	uint64_t chunk_start = 0;

	for (;;) {
		struct frame *f;
		int64_t t0, t1;

		pthread_mutex_lock(&q->m);
		while (!q->head && !q->done)
			pthread_cond_wait(&q->cv, &q->m);
		f = q->head;
		if (f) {
			q->head = f->next;
			if (!q->head)
				q->tail = NULL;
			q->bytes -= f->len;
			q->frames--;
		}
		pthread_mutex_unlock(&q->m);
		if (!f)
			break;              /* done and drained */

		{
			struct idx_rec rec = {
				.offset = offset, .ts_us = f->ts_us, .seq = f->seq,
				.bytes = (uint32_t)f->len, .flags = f->flags, .reserved = 0,
			};
			int64_t ts_le = f->ts_us;

			t0 = now_mono_us();
			if (write_all(w->vid_fd, f->data, f->len) ||
			    write_all(w->ts_fd, &ts_le, sizeof(ts_le)) ||
			    write_all(w->idx_fd, &rec, sizeof(rec))) {
				if (!w->error)
					fprintf(stderr, "write failed: %s (disk full?)\n", strerror(errno));
				w->error = 1;
			}
			t1 = now_mono_us();
			if ((double)(t1 - t0) / 1000.0 > w->write_max_ms)
				w->write_max_ms = (double)(t1 - t0) / 1000.0;
		}
		offset += f->len;
		w->written_frames++;
		w->written_bytes += f->len;
		free(f->data);
		free(f);

		/* Steady writeback: kick the last chunk out to the card as soon as
		 * it is complete, and drop the chunk before it from the page cache,
		 * so dirty data never piles up into a multi-hundred-ms flush. */
		if (offset - chunk_start >= w->wb_chunk) {
			sync_file_range(w->vid_fd, (off64_t)chunk_start, (off64_t)(offset - chunk_start),
					SYNC_FILE_RANGE_WRITE);
			if (chunk_start >= w->wb_chunk)
				posix_fadvise(w->vid_fd, 0, (off_t)(chunk_start - w->wb_chunk), POSIX_FADV_DONTNEED);
			chunk_start = offset;
		}
	}
	fdatasync(w->vid_fd);
	fdatasync(w->ts_fd);
	fdatasync(w->idx_fd);
	return NULL;
}

/*
 * Resolve --device. Accepts a path, or "port:<spec>" which matches the USB
 * port in /dev/v4l/by-path names, e.g. "port:1.3" matches
 * ...-usb-0:1.3:1.0-video-index0. The by-path name is stable across boots
 * for a given physical hub port; /dev/videoN is not.
 */
static int resolve_device(const char *spec, char *out, size_t outlen)
{
	glob_t g;
	size_t i;
	int found = 0;
	char needle[128];

	if (strncmp(spec, "port:", 5) != 0) {
		snprintf(out, outlen, "%s", spec);
		return 0;
	}
	snprintf(needle, sizeof(needle), ":%s:", spec + 5);
	if (glob("/dev/v4l/by-path/*-usb-*-video-index0", 0, NULL, &g) != 0) {
		fprintf(stderr, "ewego-cam: no USB cameras under /dev/v4l/by-path\n");
		return -1;
	}
	for (i = 0; i < g.gl_pathc; i++) {
		if (strstr(g.gl_pathv[i], needle)) {
			snprintf(out, outlen, "%s", g.gl_pathv[i]);
			found++;
		}
	}
	globfree(&g);
	if (found != 1) {
		fprintf(stderr, "ewego-cam: %s matched %d cameras (want exactly 1); use --list\n", spec, found);
		return -1;
	}
	return 0;
}

static void list_cameras(void)
{
	glob_t g;
	size_t i;

	if (glob("/dev/v4l/by-path/*-usb-*-video-index0", 0, NULL, &g) != 0) {
		printf("no USB cameras\n");
		return;
	}
	for (i = 0; i < g.gl_pathc; i++) {
		char real[PATH_MAX];
		const char *p = strstr(g.gl_pathv[i], "-usb-");
		char port[64] = "?";
		if (p) {
			/* -usb-0:1.3:1.0-video → port spec is between the first and last ':' */
			const char *a = strchr(p + 5, ':');
			const char *b = a ? strstr(a + 1, "-video") : NULL;
			if (a && b) {
				const char *c = b;
				while (c > a && *c != ':')
					c--;
				if (c > a + 1)
					snprintf(port, sizeof(port), "%.*s", (int)(c - a - 1), a + 1);
			}
		}
		if (!realpath(g.gl_pathv[i], real))
			snprintf(real, sizeof(real), "?");
		printf("port:%-12s %s  (%s)\n", port, real, g.gl_pathv[i]);
	}
	globfree(&g);
}

static void usage(FILE *f)
{
	fprintf(f,
		"ewego-cam %s — record a UVC camera with kernel timestamps\n"
		"usage: ewego-cam --device DEV|port:SPEC --out DIR [options]\n"
		"  --device DEV        /dev/videoN, a by-path link, or port:1.3 (USB hub port)\n"
		"  --out DIR           output directory (a session subdirectory is created)\n"
		"  --name NAME         file prefix (default camera1)\n"
		"  --size WxH          default 1920x1080\n"
		"  --fps N             default 30\n"
		"  --buffers N         V4L2 buffers to queue (default 16, max %d)\n"
		"  --ring MB           RAM queue between capture and disk (default 64)\n"
		"  --wb MB             start writeback every MB written (default 4)\n"
		"  --seconds N         stop after N seconds (default 0 = until SIGINT/SIGTERM)\n"
		"  --stats N           print statistics every N seconds (default 5, 0 = off)\n"
		"  --stall N           exit 3 after N seconds without a frame (default 10)\n"
		"  --no-session-dir    write directly into --out\n"
		"  --rt                SCHED_FIFO priority 10 and mlockall for the capture thread (needs root)\n"
		"  --quiet             no per-interval statistics on stdout\n"
		"  --list              list USB cameras and exit\n",
		VERSION, MAX_BUFFERS);
}

static int parse_size(const char *s, int *w, int *h)
{
	return sscanf(s, "%dx%d", w, h) == 2 && *w > 0 && *h > 0 ? 0 : -1;
}

static void stats_add_interval(struct stats *st, double dt)
{
	if (st->dt_n == 0 || dt < st->dt_min)
		st->dt_min = dt;
	if (dt > st->dt_max)
		st->dt_max = dt;
	st->dt_sum += dt;
	st->dt_n++;
	if (st->i_dt_n == 0 || dt < st->i_dt_min)
		st->i_dt_min = dt;
	if (dt > st->i_dt_max)
		st->i_dt_max = dt;
	st->i_dt_sum += dt;
	st->i_dt_n++;
}

static void print_stats(const struct cfg *c, struct stats *st, struct queue *q, struct writer *w, double elapsed)
{
	char line[320];
	double fps = st->i_dt_n ? 1e6 / (st->i_dt_sum / (double)st->i_dt_n) : 0.0;
	size_t qbytes, qhw;
	uint64_t qovr;

	pthread_mutex_lock(&q->m);
	qbytes = q->bytes;
	qhw = q->high_water;
	qovr = q->overruns;
	pthread_mutex_unlock(&q->m);

	snprintf(line, sizeof(line),
		 "%s: %" PRIu64 " frames, %" PRIu64 " lost, %" PRIu64 " err | %.2f fps, interval %.1f/%.1f/%.1f ms, "
		 "%.1f MB/s | queue %.1f MB (peak %.1f), overruns %" PRIu64 ", write max %.0f ms",
		 c->name, st->frames, st->lost, st->errors, fps,
		 st->i_dt_n ? st->i_dt_min / 1000.0 : 0.0,
		 st->i_dt_n ? st->i_dt_sum / (double)st->i_dt_n / 1000.0 : 0.0,
		 st->i_dt_n ? st->i_dt_max / 1000.0 : 0.0,
		 c->stats_interval > 0 ? (double)st->i_bytes / 1e6 / (double)c->stats_interval : 0.0,
		 (double)qbytes / 1e6, (double)qhw / 1e6, qovr, w->write_max_ms);
	if (!c->quiet)
		printf("[%7.1fs] %s\n", elapsed, line);
	{
		char msg[360];
		snprintf(msg, sizeof(msg), "STATUS=%s", line);
		sd_notify(msg);
	}
	st->i_bytes = 0;
	st->i_dt_n = 0;
	st->i_dt_sum = 0;
}

static int write_summary(const char *path, const struct cfg *c, const struct stats *st,
			 const struct queue *q, const struct writer *w,
			 const char *devpath, int64_t mono0, int64_t real0, int64_t mono1, int64_t real1,
			 int exit_code, uint32_t tstamp_flags)
{
	FILE *f = fopen(path, "w");
	if (!f)
		return -1;
	fprintf(f,
		"{\n"
		"  \"program\": \"ewego-cam %s\",\n"
		"  \"name\": \"%s\",\n"
		"  \"device\": \"%s\",\n"
		"  \"requested\": {\"width\": %d, \"height\": %d, \"fps\": %d, \"format\": \"MJPG\"},\n"
		"  \"frames\": %" PRIu64 ",\n"
		"  \"bytes\": %" PRIu64 ",\n"
		"  \"lost\": %" PRIu64 ",\n"
		"  \"gaps\": %" PRIu64 ",\n"
		"  \"gap_list\": \"%s\",\n"
		"  \"error_frames\": %" PRIu64 ",\n"
		"  \"stalls\": %" PRIu64 ",\n"
		"  \"writer\": {\"frames\": %" PRIu64 ", \"bytes\": %" PRIu64 ", \"overruns\": %" PRIu64
		", \"queue_peak_bytes\": %zu, \"queue_budget_bytes\": %zu, \"write_max_ms\": %.1f, \"error\": %d},\n"
		"  \"first_seq\": %u,\n"
		"  \"last_seq\": %u,\n"
		"  \"first_ts_us\": %" PRId64 ",\n"
		"  \"last_ts_us\": %" PRId64 ",\n"
		"  \"interval_us\": {\"min\": %.1f, \"mean\": %.1f, \"max\": %.1f, \"n\": %" PRIu64 "},\n"
		"  \"effective_fps\": %.3f,\n"
		"  \"timestamp_clock\": \"%s\",\n"
		"  \"timestamp_source\": \"%s\",\n"
		"  \"clock_at_start\": {\"monotonic_us\": %" PRId64 ", \"realtime_us\": %" PRId64 "},\n"
		"  \"clock_at_end\": {\"monotonic_us\": %" PRId64 ", \"realtime_us\": %" PRId64 "},\n"
		"  \"realtime_minus_monotonic_us\": %" PRId64 ",\n"
		"  \"exit_code\": %d\n"
		"}\n",
		VERSION, c->name, devpath, c->width, c->height, c->fps,
		st->frames, st->bytes, st->lost, st->gaps, st->gap_list, st->errors, st->stalls,
		w->written_frames, w->written_bytes, q->overruns, q->high_water, q->max_bytes,
		w->write_max_ms, w->error,
		st->have_seq ? st->first_seq : 0, st->have_seq ? st->last_seq : 0,
		st->first_ts, st->last_ts,
		st->dt_n ? st->dt_min : 0.0, st->dt_n ? st->dt_sum / (double)st->dt_n : 0.0,
		st->dt_n ? st->dt_max : 0.0, st->dt_n,
		(st->frames > 1 && st->last_ts > st->first_ts)
			? (double)(st->frames - 1) * 1e6 / (double)(st->last_ts - st->first_ts) : 0.0,
		(tstamp_flags & V4L2_BUF_FLAG_TIMESTAMP_MASK) == V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC ? "monotonic"
			: (tstamp_flags & V4L2_BUF_FLAG_TIMESTAMP_MASK) == V4L2_BUF_FLAG_TIMESTAMP_COPY ? "copy" : "unknown",
		(tstamp_flags & V4L2_BUF_FLAG_TSTAMP_SRC_MASK) == V4L2_BUF_FLAG_TSTAMP_SRC_SOE
			? "start-of-exposure (as flagged by uvcvideo; arrival time unless hwtimestamps=1)"
			: "end-of-frame/arrival",
		mono0, real0, mono1, real1, real0 - mono0, exit_code);
	fclose(f);
	return 0;
}

static int open_out(const char *dir, const char *name, const char *suffix)
{
	char path[PATH_MAX + 64];
	int fd;
	snprintf(path, sizeof(path), "%s/%s%s", dir, name, suffix);
	fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
	if (fd < 0)
		perror(path);
	return fd;
}

int main(int argc, char **argv)
{
	struct cfg c = {
		.device = NULL, .out = NULL, .name = "camera1",
		.width = 1920, .height = 1080, .fps = 30, .buffers = 16,
		.seconds = 0, .stats_interval = 5, .stall_timeout = 10,
		.ring_bytes = 64u << 20, .wb_chunk = 4u << 20,
		.session_dir = 1, .realtime = 0, .quiet = 0,
	};
	static const struct option longopts[] = {
		{"device", required_argument, 0, 'd'}, {"out", required_argument, 0, 'o'},
		{"name", required_argument, 0, 'n'},   {"size", required_argument, 0, 's'},
		{"fps", required_argument, 0, 'f'},    {"buffers", required_argument, 0, 'b'},
		{"ring", required_argument, 0, 'R'},   {"wb", required_argument, 0, 'W'},
		{"seconds", required_argument, 0, 't'}, {"stats", required_argument, 0, 'S'},
		{"sync", required_argument, 0, 'y'},   /* accepted for compatibility, ignored */
		{"stall", required_argument, 0, 'x'},
		{"no-session-dir", no_argument, 0, 'N'}, {"rt", no_argument, 0, 'r'},
		{"quiet", no_argument, 0, 'q'},        {"list", no_argument, 0, 'l'},
		{"help", no_argument, 0, 'h'},         {0, 0, 0, 0},
	};
	int opt;
	char devpath[PATH_MAX];
	char dir[PATH_MAX], path[PATH_MAX + 64];
	int fd = -1;
	void *bufs[MAX_BUFFERS] = {0};
	size_t buflen[MAX_BUFFERS] = {0};
	struct stats st;
	struct queue q;
	struct writer w;
	pthread_t writer_thread;
	int64_t mono0, real0, t_start, t_last_stats, t_last_frame;
	uint32_t tstamp_flags = 0;
	int exit_code = 0;
	unsigned int i;

	memset(&st, 0, sizeof(st));
	memset(&q, 0, sizeof(q));
	memset(&w, 0, sizeof(w));

	while ((opt = getopt_long(argc, argv, "d:o:n:s:f:b:R:W:t:S:y:x:Nrqlh", longopts, NULL)) != -1) {
		switch (opt) {
		case 'd': c.device = optarg; break;
		case 'o': c.out = optarg; break;
		case 'n': c.name = optarg; break;
		case 's':
			if (parse_size(optarg, &c.width, &c.height)) {
				fprintf(stderr, "bad --size %s\n", optarg);
				return 1;
			}
			break;
		case 'f': c.fps = atoi(optarg); break;
		case 'b': c.buffers = atoi(optarg); break;
		case 'R': c.ring_bytes = (size_t)atoi(optarg) << 20; break;
		case 'W': c.wb_chunk = (size_t)atoi(optarg) << 20; break;
		case 't': c.seconds = atoi(optarg); break;
		case 'S': c.stats_interval = atoi(optarg); break;
		case 'y': break;
		case 'x': c.stall_timeout = atoi(optarg); break;
		case 'N': c.session_dir = 0; break;
		case 'r': c.realtime = 1; break;
		case 'q': c.quiet = 1; break;
		case 'l': list_cameras(); return 0;
		case 'h': usage(stdout); return 0;
		default: usage(stderr); return 1;
		}
	}
	if (!c.device || !c.out) {
		usage(stderr);
		return 1;
	}
	if (c.buffers < 2 || c.buffers > MAX_BUFFERS) {
		fprintf(stderr, "--buffers must be 2..%d\n", MAX_BUFFERS);
		return 1;
	}
	if (c.fps <= 0 || c.ring_bytes < (1u << 20) || c.wb_chunk < (1u << 16)) {
		fprintf(stderr, "--fps must be > 0, --ring >= 1, --wb >= 1\n");
		return 1;
	}

	if (resolve_device(c.device, devpath, sizeof(devpath)))
		return 1;

	/* output directory */
	if (c.session_dir) {
		time_t t = time(NULL);
		struct tm tm;
		char stamp[32];
		localtime_r(&t, &tm);
		strftime(stamp, sizeof(stamp), "%Y%m%d_%H%M%S", &tm);
		snprintf(dir, sizeof(dir), "%s/%s", c.out, stamp);
	} else {
		snprintf(dir, sizeof(dir), "%s", c.out);
	}
	if (mkdir(c.out, 0755) && errno != EEXIST) {
		perror(c.out);
		return 1;
	}
	if (mkdir(dir, 0755) && errno != EEXIST) {
		perror(dir);
		return 1;
	}

	/* open camera and configure */
	fd = open(devpath, O_RDWR | O_CLOEXEC);
	if (fd < 0) {
		fprintf(stderr, "open %s: %s\n", devpath, strerror(errno));
		return 1;
	}
	{
		struct v4l2_capability cap;
		memset(&cap, 0, sizeof(cap));
		if (xioctl(fd, VIDIOC_QUERYCAP, &cap)) {
			perror("VIDIOC_QUERYCAP");
			return 1;
		}
		uint32_t caps = (cap.capabilities & V4L2_CAP_DEVICE_CAPS) ? cap.device_caps : cap.capabilities;
		if (!(caps & V4L2_CAP_VIDEO_CAPTURE) || !(caps & V4L2_CAP_STREAMING)) {
			fprintf(stderr, "%s is not a streaming video capture device (caps 0x%x)\n", devpath, caps);
			return 1;
		}
		fprintf(stderr, "ewego-cam %s: %s (%s, driver %s)\n", VERSION, devpath, cap.card, cap.driver);
	}
	{
		struct v4l2_format fmt;
		memset(&fmt, 0, sizeof(fmt));
		fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
		fmt.fmt.pix.width = (uint32_t)c.width;
		fmt.fmt.pix.height = (uint32_t)c.height;
		fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_MJPEG;
		fmt.fmt.pix.field = V4L2_FIELD_NONE;
		if (xioctl(fd, VIDIOC_S_FMT, &fmt)) {
			perror("VIDIOC_S_FMT");
			return 1;
		}
		if (fmt.fmt.pix.pixelformat != V4L2_PIX_FMT_MJPEG) {
			fprintf(stderr, "camera does not offer MJPEG\n");
			return 1;
		}
		if ((int)fmt.fmt.pix.width != c.width || (int)fmt.fmt.pix.height != c.height)
			fprintf(stderr, "note: camera chose %ux%u instead of %dx%d\n",
				fmt.fmt.pix.width, fmt.fmt.pix.height, c.width, c.height);
		c.width = (int)fmt.fmt.pix.width;
		c.height = (int)fmt.fmt.pix.height;
	}
	{
		struct v4l2_streamparm parm;
		memset(&parm, 0, sizeof(parm));
		parm.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
		parm.parm.capture.timeperframe.numerator = 1;
		parm.parm.capture.timeperframe.denominator = (uint32_t)c.fps;
		if (xioctl(fd, VIDIOC_S_PARM, &parm))
			fprintf(stderr, "note: VIDIOC_S_PARM failed (%s); using camera default rate\n", strerror(errno));
		else if (parm.parm.capture.timeperframe.numerator &&
			 (int)(parm.parm.capture.timeperframe.denominator / parm.parm.capture.timeperframe.numerator) != c.fps)
			fprintf(stderr, "note: camera chose %u/%u s per frame instead of 1/%d\n",
				parm.parm.capture.timeperframe.numerator, parm.parm.capture.timeperframe.denominator, c.fps);
	}
	{
		struct v4l2_requestbuffers req;
		memset(&req, 0, sizeof(req));
		req.count = (uint32_t)c.buffers;
		req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
		req.memory = V4L2_MEMORY_MMAP;
		if (xioctl(fd, VIDIOC_REQBUFS, &req)) {
			perror("VIDIOC_REQBUFS");
			return 1;
		}
		if (req.count < 2) {
			fprintf(stderr, "driver gave only %u buffers\n", req.count);
			return 1;
		}
		c.buffers = (int)req.count;
		for (i = 0; i < req.count; i++) {
			struct v4l2_buffer b;
			memset(&b, 0, sizeof(b));
			b.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
			b.memory = V4L2_MEMORY_MMAP;
			b.index = i;
			if (xioctl(fd, VIDIOC_QUERYBUF, &b)) {
				perror("VIDIOC_QUERYBUF");
				return 1;
			}
			bufs[i] = mmap(NULL, b.length, PROT_READ | PROT_WRITE, MAP_SHARED, fd, b.m.offset);
			if (bufs[i] == MAP_FAILED) {
				perror("mmap");
				return 1;
			}
			buflen[i] = b.length;
			if (xioctl(fd, VIDIOC_QBUF, &b)) {
				perror("VIDIOC_QBUF");
				return 1;
			}
		}
	}

	/* output files and writer thread */
	w.vid_fd = open_out(dir, c.name, ".mjpeg");
	w.ts_fd = open_out(dir, c.name, "_timestamps.bin");
	w.idx_fd = open_out(dir, c.name, "_index.bin");
	if (w.vid_fd < 0 || w.ts_fd < 0 || w.idx_fd < 0)
		return 1;
	pthread_mutex_init(&q.m, NULL);
	pthread_cond_init(&q.cv, NULL);
	q.max_bytes = c.ring_bytes;
	w.q = &q;
	w.wb_chunk = c.wb_chunk;
	if (pthread_create(&writer_thread, NULL, writer_main, &w)) {
		perror("pthread_create");
		return 1;
	}

	if (c.realtime) {
		struct sched_param sp = {.sched_priority = 10};
		if (sched_setscheduler(0, SCHED_FIFO, &sp))
			fprintf(stderr, "note: SCHED_FIFO not granted (%s)\n", strerror(errno));
		if (mlockall(MCL_CURRENT | MCL_FUTURE))
			fprintf(stderr, "note: mlockall failed (%s)\n", strerror(errno));
	}

	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);
	signal(SIGPIPE, SIG_IGN);

	{
		enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
		if (xioctl(fd, VIDIOC_STREAMON, &type)) {
			perror("VIDIOC_STREAMON");
			return 1;
		}
	}
	mono0 = now_mono_us();
	real0 = now_real_us();
	t_start = t_last_stats = t_last_frame = mono0;
	fprintf(stderr, "recording %dx%d MJPG @ %d fps, %d buffers, %zu MB queue -> %s/%s.*\n",
		c.width, c.height, c.fps, c.buffers, c.ring_bytes >> 20, dir, c.name);
	sd_notify("READY=1");

	/* capture loop: dequeue, copy into the queue, requeue. No disk I/O here. */
	while (!g_stop) {
		struct pollfd pfd = {.fd = fd, .events = POLLIN};
		struct v4l2_buffer b;
		int64_t now, ts_us;
		int r;

		r = poll(&pfd, 1, 1000);
		now = now_mono_us();
		if (r < 0) {
			if (errno == EINTR)
				continue;
			perror("poll");
			exit_code = 1;
			break;
		}
		if (r == 0 || !(pfd.revents & POLLIN)) {
			if (pfd.revents & POLLERR) {
				fprintf(stderr, "poll: device error\n");
				exit_code = 1;
				break;
			}
			if (now - t_last_frame > (int64_t)c.stall_timeout * 1000000) {
				fprintf(stderr, "no frame for %d s: camera stalled\n", c.stall_timeout);
				st.stalls++;
				exit_code = 3;
				break;
			}
		} else {
			memset(&b, 0, sizeof(b));
			b.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
			b.memory = V4L2_MEMORY_MMAP;
			if (xioctl(fd, VIDIOC_DQBUF, &b)) {
				if (errno == EAGAIN)
					continue;
				perror("VIDIOC_DQBUF");
				exit_code = 1;
				break;
			}
			ts_us = (int64_t)b.timestamp.tv_sec * 1000000 + b.timestamp.tv_usec;
			tstamp_flags = b.flags;
			t_last_frame = now;

			if (!st.have_seq) {
				st.first_seq = b.sequence;
				st.first_ts = ts_us;
				st.have_seq = 1;
			} else {
				if (b.sequence != st.last_seq + 1) {
					uint32_t missing = b.sequence - st.last_seq - 1;
					st.gaps++;
					st.lost += missing;
					if (strlen(st.gap_list) < sizeof(st.gap_list) - 32) {
						char g[32];
						snprintf(g, sizeof(g), "%s%u->%u", st.gap_list[0] ? "," : "", st.last_seq, b.sequence);
						strncat(st.gap_list, g, sizeof(st.gap_list) - strlen(st.gap_list) - 1);
					}
					if (!c.quiet)
						fprintf(stderr, "gap: seq %u -> %u (%u lost)\n", st.last_seq, b.sequence, missing);
				}
				if (ts_us > st.last_ts)
					stats_add_interval(&st, (double)(ts_us - st.last_ts));
				else if (!c.quiet)
					fprintf(stderr, "warning: timestamp went backwards (%" PRId64 " -> %" PRId64 " us)\n",
						st.last_ts, ts_us);
			}
			st.last_seq = b.sequence;
			st.last_ts = ts_us;
			if (b.flags & V4L2_BUF_FLAG_ERROR)
				st.errors++;

			if (b.bytesused > 0 && b.bytesused <= buflen[b.index]) {
				struct frame *f = NULL;
				int overrun = 0;

				pthread_mutex_lock(&q.m);
				if (q.bytes + b.bytesused > q.max_bytes) {
					q.overruns++;
					overrun = 1;
				}
				pthread_mutex_unlock(&q.m);

				if (!overrun) {
					f = malloc(sizeof(*f));
					if (f) {
						f->data = malloc(b.bytesused);
						if (!f->data) {
							free(f);
							f = NULL;
						}
					}
					if (!f) {
						overrun = 1;
						pthread_mutex_lock(&q.m);
						q.overruns++;
						pthread_mutex_unlock(&q.m);
					}
				}
				if (f) {
					memcpy(f->data, bufs[b.index], b.bytesused);
					f->len = b.bytesused;
					f->ts_us = ts_us;
					f->seq = b.sequence;
					f->flags = b.flags;
					f->next = NULL;
					pthread_mutex_lock(&q.m);
					if (q.tail)
						q.tail->next = f;
					else
						q.head = f;
					q.tail = f;
					q.bytes += f->len;
					q.frames++;
					if (q.bytes > q.high_water)
						q.high_water = q.bytes;
					pthread_cond_signal(&q.cv);
					pthread_mutex_unlock(&q.m);
				} else if (!c.quiet) {
					fprintf(stderr, "writer queue full: frame seq %u not written\n", b.sequence);
				}
				st.frames++;
				st.bytes += b.bytesused;
				st.i_bytes += b.bytesused;
			}
			if (xioctl(fd, VIDIOC_QBUF, &b)) {
				perror("VIDIOC_QBUF");
				exit_code = 1;
				break;
			}
		}

		if (c.stats_interval > 0 && now - t_last_stats >= (int64_t)c.stats_interval * 1000000) {
			print_stats(&c, &st, &q, &w, (double)(now - t_start) / 1e6);
			t_last_stats = now;
		}
		if (c.seconds > 0 && now - t_start >= (int64_t)c.seconds * 1000000)
			break;
	}

	/* shutdown: stop the stream, then let the writer drain the queue */
	sd_notify("STOPPING=1");
	{
		enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
		xioctl(fd, VIDIOC_STREAMOFF, &type);
	}
	for (i = 0; i < (unsigned int)c.buffers; i++)
		if (bufs[i])
			munmap(bufs[i], buflen[i]);
	close(fd);

	pthread_mutex_lock(&q.m);
	q.done = 1;
	pthread_cond_signal(&q.cv);
	pthread_mutex_unlock(&q.m);
	pthread_join(writer_thread, NULL);
	close(w.vid_fd);
	close(w.ts_fd);
	close(w.idx_fd);

	if (exit_code == 0 && (st.lost || st.errors || q.overruns || w.error))
		exit_code = 2;
	{
		int64_t mono1 = now_mono_us(), real1 = now_real_us();
		snprintf(path, sizeof(path), "%s/%s_summary.json", dir, c.name);
		write_summary(path, &c, &st, &q, &w, devpath, mono0, real0, mono1, real1, exit_code, tstamp_flags);
	}
	fprintf(stderr, "%s: %" PRIu64 " frames, %" PRIu64 " lost in %" PRIu64 " gap(s), %" PRIu64 " error-flagged, "
		"%" PRIu64 " queue overruns, %.1f MB, interval min/mean/max %.1f/%.1f/%.1f ms, "
		"queue peak %.1f MB, write max %.0f ms -> exit %d\n",
		c.name, st.frames, st.lost, st.gaps, st.errors, q.overruns, (double)w.written_bytes / 1e6,
		st.dt_n ? st.dt_min / 1000.0 : 0.0, st.dt_n ? st.dt_sum / (double)st.dt_n / 1000.0 : 0.0,
		st.dt_n ? st.dt_max / 1000.0 : 0.0, (double)q.high_water / 1e6, w.write_max_ms, exit_code);
	return exit_code;
}
