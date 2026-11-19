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
 * --live DIR (meant for tmpfs, /run/ewego) keeps <name>.jpg = the newest
 * frame, replaced at most 5 times a second, and <name>.json = counters,
 * rewritten once a second, for the web console's live view. --preview
 * streams the camera into the live files only and records nothing.
 *
 * Exit status: 0 clean and no frames lost; 2 frames lost, error-flagged, or
 * dropped by a full write queue; 3 camera stalled; 1 usage or device error;
 * 4 output error (cannot create or write the files: card full, I/O error),
 * which stops the recording at once instead of running on unrecorded.
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
#include <stdarg.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include <linux/videodev2.h>

#define VERSION "0.6.0"
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
	int auto_fps;        /* leave the camera's exposure-auto-priority on */
	int wait_secs;       /* wait this long for the device to appear */
	const char *live;    /* directory for the live frame + counters, or NULL */
	int preview;         /* live files only, no recording */
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
	/* written by the capture thread, read by the writer, both under m */
	struct stats st;
	uint32_t tstamp_flags;
};

/*
 * One output file's writeback state. Every step waits for (and checks) the
 * pages kicked out by the previous step, then kicks out what was appended
 * since; so the wait is for data already on its way to the card and does not
 * stall, and an I/O error on the card is reported (SYNC_FILE_RANGE_WRITE
 * alone never reports one).
 */
struct wbfile {
	int fd;
	uint64_t size;      /* bytes written to this file (= last good boundary) */
	uint64_t kicked;    /* writeback started up to here */
	uint64_t waited;    /* written back and checked up to here (page aligned) */
	int unsupported;    /* sync_file_range not available on this file */
};

struct writer {
	struct queue *q;
	int vid_fd, ts_fd, idx_fd;   /* -1 in --preview: nothing is recorded */
	struct wbfile wb[3];         /* video, timestamps, index */
	size_t wb_chunk;
	int64_t wb_last_us;          /* time of the last writeback step */
	const char *live, *name;     /* live frame: <live>/<name>.jpg */
	int64_t live_last_ts;        /* -1 until the first live frame is written */
	/* results */
	uint64_t written_frames, written_bytes;
	uint64_t discarded;          /* frames dropped after an output error */
	double write_max_ms;
	int error;                   /* errno of the first output error, 0 = none */
	char error_what[96];         /* which call failed */
	/* periodic work (stats line, STATUS=, live JSON, in-progress summary) */
	const struct cfg *c;
	const char *devpath, *port;
	const char *dir;             /* session directory, NULL in --preview */
	int64_t t_start, mono0, real0;
	int64_t t_last_stats, t_last_live, t_last_summary;
	uint64_t live_frames0;
	int summary_written, summary_failed;
};

#define WB_MAX_INTERVAL_US 2000000   /* writeback step at least this often */

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


/* mkdir -p: every component, EEXIST is fine. Returns 0 or -1 with errno. */
static int mkdir_p(const char *path, mode_t mode)
{
	char buf[PATH_MAX];
	size_t len = strlen(path), i;

	if (len == 0 || len >= sizeof(buf))
		return -1;
	memcpy(buf, path, len + 1);
	for (i = 1; i <= len; i++) {
		if (buf[i] == '/' || buf[i] == 0) {
			char c = buf[i];
			buf[i] = 0;
			if (mkdir(buf, mode) && errno != EEXIST)
				return -1;
			buf[i] = c;
		}
	}
	return 0;
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

/* Free space for the recorder (f_bavail) on the filesystem holding dir, MB; -1 if unknown. */
static double free_mb(const char *dir)
{
	struct statvfs v;

	if (!dir || !dir[0] || statvfs(dir, &v))
		return -1.0;
	return (double)v.f_bavail * (double)v.f_frsize / 1e6;
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

/* First output error wins; the capture loop stops and the process exits 4. */
static void writer_fail(struct writer *w, int err, const char *what)
{
	if (w->error)
		return;
	w->error = err ? err : EIO;
	snprintf(w->error_what, sizeof(w->error_what), "%s", what);
	fprintf(stderr, "ewego-cam: output error: %s: %s; stopping (exit 4)\n", what, strerror(w->error));
	g_stop = 1;
}

/* One writeback step for one file (see struct wbfile). Returns 0 or -1 with errno. */
static int wb_step(struct wbfile *f, int drop_cache)
{
	static uint64_t page;
	uint64_t wend;

	if (f->fd < 0 || f->unsupported)
		return 0;
	if (!page)
		page = (uint64_t)sysconf(_SC_PAGESIZE);
	/* the last, partial page is still being appended to: leave it for the next step */
	wend = f->kicked & ~(page - 1);
	if (wend > f->waited) {
		if (sync_file_range(f->fd, (off64_t)f->waited, (off64_t)(wend - f->waited),
				    SYNC_FILE_RANGE_WAIT_BEFORE | SYNC_FILE_RANGE_WRITE | SYNC_FILE_RANGE_WAIT_AFTER)) {
			if (errno == ESPIPE || errno == EINVAL || errno == ENOSYS) {
				f->unsupported = 1;
				return 0;
			}
			return -1;
		}
		f->waited = wend;
		/* written back and clean: drop it from the page cache */
		if (drop_cache)
			posix_fadvise(f->fd, 0, (off_t)wend, POSIX_FADV_DONTNEED);
	}
	if (f->size > f->kicked) {
		if (sync_file_range(f->fd, (off64_t)f->kicked, (off64_t)(f->size - f->kicked), SYNC_FILE_RANGE_WRITE)) {
			if (errno == ESPIPE || errno == EINVAL || errno == ENOSYS) {
				f->unsupported = 1;
				return 0;
			}
			return -1;
		}
		f->kicked = f->size;
	}
	return 0;
}

/*
 * Steady writeback of all three files at the same cadence (every --wb MB of
 * video or WB_MAX_INTERVAL_US, whichever comes first), so the index and the
 * timestamps lag the video on the card by the same few seconds instead of
 * sitting in the page cache until dirty_expire (~30 s).
 */
static void writer_writeback(struct writer *w, int64_t now)
{
	static const char *const what[3] = {"writeback .mjpeg", "writeback _timestamps.bin", "writeback _index.bin"};
	int i;

	w->wb_last_us = now;
	for (i = 0; i < 3 && !w->error; i++)
		if (wb_step(&w->wb[i], i == 0))
			writer_fail(w, errno, what[i]);
}

/*
 * Append one frame to the three files. On a failed or short write every file
 * is cut back to the last good frame boundary, so the files stay consistent
 * (every index record points at a whole frame) whatever the card did.
 */
static int writer_put(struct writer *w, const struct frame *f)
{
	struct idx_rec rec = {
		.offset = w->wb[0].size, .ts_us = f->ts_us, .seq = f->seq,
		.bytes = (uint32_t)f->len, .flags = f->flags, .reserved = 0,
	};
	int64_t ts_le = f->ts_us;
	const char *what = NULL;
	int err, i;

	if (write_all(w->vid_fd, f->data, f->len))
		what = "write .mjpeg";
	else if (write_all(w->ts_fd, &ts_le, sizeof(ts_le)))
		what = "write _timestamps.bin";
	else if (write_all(w->idx_fd, &rec, sizeof(rec)))
		what = "write _index.bin";
	if (!what) {
		w->wb[0].size += f->len;
		w->wb[1].size += sizeof(ts_le);
		w->wb[2].size += sizeof(rec);
		return 0;
	}
	err = errno;
	for (i = 0; i < 3; i++)
		if (ftruncate(w->wb[i].fd, (off_t)w->wb[i].size) == 0)
			lseek(w->wb[i].fd, (off_t)w->wb[i].size, SEEK_SET);
	writer_fail(w, err, what);
	return -1;
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

/* Everything the stats line, the live JSON and the summary report, copied
 * under the queue mutex so the writer thread never reads a half-updated set. */
struct snapshot {
	struct stats st;
	size_t qbytes, qhw, qmax;
	uint64_t qovr;
	uint32_t tstamp_flags;
};

static void take_snapshot(struct queue *q, struct snapshot *s, int reset_interval)
{
	pthread_mutex_lock(&q->m);
	s->st = q->st;
	s->qbytes = q->bytes;
	s->qhw = q->high_water;
	s->qmax = q->max_bytes;
	s->qovr = q->overruns;
	s->tstamp_flags = q->tstamp_flags;
	if (reset_interval) {
		q->st.i_bytes = 0;
		q->st.i_dt_n = 0;
		q->st.i_dt_sum = 0;
	}
	pthread_mutex_unlock(&q->m);
}

static void print_stats(const struct cfg *c, const struct snapshot *s, const struct writer *w,
			const char *dir, double elapsed)
{
	const struct stats *st = &s->st;
	char line[400];
	double fmb = free_mb(dir);
	double fps = st->i_dt_n ? 1e6 / (st->i_dt_sum / (double)st->i_dt_n) : 0.0;

	snprintf(line, sizeof(line),
		 "%s: %" PRIu64 " frames, %" PRIu64 " lost, %" PRIu64 " err | %.2f fps, interval %.1f/%.1f/%.1f ms, "
		 "%.1f MB/s | queue %.1f MB (peak %.1f), overruns %" PRIu64 ", write max %.0f ms | free %.0f MB%s%s",
		 c->name, st->frames, st->lost, st->errors, fps,
		 st->i_dt_n ? st->i_dt_min / 1000.0 : 0.0,
		 st->i_dt_n ? st->i_dt_sum / (double)st->i_dt_n / 1000.0 : 0.0,
		 st->i_dt_n ? st->i_dt_max / 1000.0 : 0.0,
		 c->stats_interval > 0 ? (double)st->i_bytes / 1e6 / (double)c->stats_interval : 0.0,
		 (double)s->qbytes / 1e6, (double)s->qhw / 1e6, s->qovr, w->write_max_ms, fmb < 0 ? 0.0 : fmb,
		 w->error ? " | OUTPUT ERROR: " : "", w->error ? w->error_what : "");
	if (!c->quiet)
		printf("[%7.1fs] %s\n", elapsed, line);
	{
		char msg[420];
		snprintf(msg, sizeof(msg), "STATUS=%s", line);
		sd_notify(msg);
	}
}

/* Live counters for the console: rewritten once a second, atomic replace. */
static void write_live_json(const struct cfg *c, const struct snapshot *s, const struct writer *w,
			    int64_t now, double fps_recent)
{
	const struct stats *st = &s->st;
	char tmp[PATH_MAX + 64], fin[PATH_MAX + 64];
	FILE *f;

	snprintf(fin, sizeof(fin), "%s/%s.json", c->live, c->name);
	snprintf(tmp, sizeof(tmp), "%s/.%s.json.tmp", c->live, c->name);
	f = fopen(tmp, "w");
	if (!f)
		return;
	fprintf(f, "{\"program\":\"ewego-cam %s\",\"name\":\"%s\",\"device\":\"%s\",\"port\":\"%s\",\"preview\":%d,",
		VERSION, c->name, w->devpath, w->port, c->preview);
	if (w->dir)
		fprintf(f, "\"session\":\"%s\",", w->dir);
	else
		fputs("\"session\":null,", f);
	fprintf(f, "\"width\":%d,\"height\":%d,\"fps_requested\":%d,\"elapsed_s\":%.1f,"
		   "\"frames\":%" PRIu64 ",\"bytes\":%" PRIu64 ",\"lost\":%" PRIu64 ",\"gaps\":%" PRIu64
		   ",\"errors\":%" PRIu64 ",\"fps\":%.2f,\"queue_mb\":%.1f,\"queue_peak_mb\":%.1f,"
		   "\"overruns\":%" PRIu64 ",\"write_max_ms\":%.0f,\"written_mb\":%.1f,"
		   "\"updated_mono_us\":%" PRId64 ",\"updated_real_us\":%" PRId64 ",",
		c->width, c->height, c->fps, (double)(now - w->t_start) / 1e6,
		st->frames, st->bytes, st->lost, st->gaps, st->errors, fps_recent,
		(double)s->qbytes / 1e6, (double)s->qhw / 1e6, s->qovr, w->write_max_ms,
		(double)w->written_bytes / 1e6, now, now_real_us());
	{
		double fmb = free_mb(w->dir);
		if (fmb < 0)
			fputs("\"free_mb\":null,", f);
		else
			fprintf(f, "\"free_mb\":%.0f,", fmb);
	}
	fprintf(f, "\"writer_error\":%d,\"writer_error_msg\":\"", w->error ? 1 : 0);
	if (w->error)
		fprintf(f, "%s: %s", w->error_what, strerror(w->error));
	fprintf(f, "\",\"discarded\":%" PRIu64 "}\n", w->discarded);
	fclose(f);
	rename(tmp, fin);
}

/*
 * <dir>/<name>_summary.json, replaced atomically (tmp + fdatasync + rename).
 * The writer thread rewrites it every stats interval with "in_progress": true
 * and "exit_code": null, so the wall-clock mapping and the counters survive a
 * power cut or SIGKILL; the final write at exit has "in_progress": false.
 */
static int write_summary(const struct cfg *c, const struct snapshot *s, const struct writer *w,
			 int64_t mono1, int64_t real1, int exit_code, int in_progress)
{
	const struct stats *st = &s->st;
	uint32_t tstamp_flags = s->tstamp_flags;
	char path[PATH_MAX + 64], tmp[PATH_MAX + 64], code[16];
	double fmb = free_mb(w->dir);
	FILE *f;
	int bad;

	snprintf(path, sizeof(path), "%s/%s_summary.json", w->dir, c->name);
	snprintf(tmp, sizeof(tmp), "%s/.%s_summary.json.tmp", w->dir, c->name);
	if (in_progress)
		snprintf(code, sizeof(code), "null");
	else
		snprintf(code, sizeof(code), "%d", exit_code);
	f = fopen(tmp, "w");
	if (!f)
		return -1;
	fprintf(f,
		"{\n"
		"  \"program\": \"ewego-cam %s\",\n"
		"  \"name\": \"%s\",\n"
		"  \"device\": \"%s\",\n"
		"  \"port\": \"%s\",\n"
		"  \"requested\": {\"width\": %d, \"height\": %d, \"fps\": %d, \"format\": \"MJPG\"},\n"
		"  \"in_progress\": %s,\n"
		"  \"frames\": %" PRIu64 ",\n"
		"  \"bytes\": %" PRIu64 ",\n"
		"  \"lost\": %" PRIu64 ",\n"
		"  \"gaps\": %" PRIu64 ",\n"
		"  \"gap_list\": \"%s\",\n"
		"  \"error_frames\": %" PRIu64 ",\n"
		"  \"stalls\": %" PRIu64 ",\n"
		"  \"writer\": {\"frames\": %" PRIu64 ", \"bytes\": %" PRIu64 ", \"overruns\": %" PRIu64
		", \"queue_peak_bytes\": %zu, \"queue_budget_bytes\": %zu, \"write_max_ms\": %.1f, \"error\": %d"
		", \"error_msg\": \"%s%s%s\", \"discarded\": %" PRIu64 "},\n"
		"  \"free_mb\": %.0f,\n"
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
		"  \"exit_code\": %s\n"
		"}\n",
		VERSION, c->name, w->devpath, w->port, c->width, c->height, c->fps,
		in_progress ? "true" : "false",
		st->frames, st->bytes, st->lost, st->gaps, st->gap_list, st->errors, st->stalls,
		w->written_frames, w->written_bytes, s->qovr, s->qhw, s->qmax,
		w->write_max_ms, w->error ? 1 : 0, w->error ? w->error_what : "", w->error ? ": " : "",
		w->error ? strerror(w->error) : "", w->discarded,
		fmb,
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
		w->mono0, w->real0, mono1, real1, w->real0 - w->mono0, code);
	bad = fflush(f) != 0 || ferror(f) || fdatasync(fileno(f)) != 0;
	if (fclose(f) != 0)
		bad = 1;
	if (bad || rename(tmp, path)) {
		unlink(tmp);
		return -1;
	}
	return 0;
}

/*
 * Periodic work, all on the writer thread so the capture thread never
 * blocks on the journal, the notify socket or a filesystem: the stats line
 * and STATUS=, the live JSON (1/s) and the in-progress summary.
 */
static void writer_housekeeping(struct writer *w, int64_t now)
{
	const struct cfg *c = w->c;
	int64_t sum_every = (int64_t)(c->stats_interval > 0 ? c->stats_interval : 5) * 1000000;
	int do_stats = c->stats_interval > 0 && now - w->t_last_stats >= (int64_t)c->stats_interval * 1000000;
	int do_live = c->live && now - w->t_last_live >= 1000000;
	int do_sum = w->dir && (!w->summary_written || now - w->t_last_summary >= sum_every);
	struct snapshot s;

	if (!do_stats && !do_live && !do_sum)
		return;
	take_snapshot(w->q, &s, do_stats);
	if (do_stats) {
		print_stats(c, &s, w, w->dir, (double)(now - w->t_start) / 1e6);
		w->t_last_stats = now;
	}
	if (do_live) {
		write_live_json(c, &s, w, now, (double)(s.st.frames - w->live_frames0) * 1e6 /
				(double)(now - w->t_last_live));
		w->live_frames0 = s.st.frames;
		w->t_last_live = now;
	}
	if (do_sum) {
		if (write_summary(c, &s, w, now, now_real_us(), 0, 1) && !w->summary_failed) {
			fprintf(stderr, "ewego-cam: could not write the in-progress summary in %s: %s\n",
				w->dir, strerror(errno));
			w->summary_failed = 1;
		}
		w->summary_written = 1;
		w->t_last_summary = now;
	}
}

static void *writer_main(void *arg)
{
	struct writer *w = arg;
	struct queue *q = w->q;
	int i;

	for (;;) {
		struct frame *f;
		int64_t t0, t1, now;
		int done;

		pthread_mutex_lock(&q->m);
		if (!q->head && !q->done) {
			/* wake at least every 250 ms for the periodic work */
			struct timespec dl;
			clock_gettime(CLOCK_MONOTONIC, &dl);
			dl.tv_nsec += 250000000;
			if (dl.tv_nsec >= 1000000000) {
				dl.tv_sec++;
				dl.tv_nsec -= 1000000000;
			}
			while (!q->head && !q->done)
				if (pthread_cond_timedwait(&q->cv, &q->m, &dl) == ETIMEDOUT)
					break;
		}
		f = q->head;
		if (f) {
			q->head = f->next;
			if (!q->head)
				q->tail = NULL;
			q->bytes -= f->len;
			q->frames--;
		}
		done = q->done;
		pthread_mutex_unlock(&q->m);
		if (!f) {
			if (done)
				break;          /* done and drained */
			now = now_mono_us();
			if (w->vid_fd >= 0 && !w->error && now - w->wb_last_us >= WB_MAX_INTERVAL_US)
				writer_writeback(w, now);   /* idle: finish the last chunk too */
			writer_housekeeping(w, now);
			continue;
		}

		/* newest frame for the live view: at most 5 a second, atomic replace */
		if (w->live && (w->live_last_ts < 0 || f->ts_us - w->live_last_ts >= 200000)) {
			char tmp[PATH_MAX + 64], fin[PATH_MAX + 64];
			int lfd;
			snprintf(fin, sizeof(fin), "%s/%s.jpg", w->live, w->name);
			snprintf(tmp, sizeof(tmp), "%s/.%s.jpg.tmp", w->live, w->name);
			lfd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
			if (lfd >= 0) {
				if (write_all(lfd, f->data, f->len) == 0)
					rename(tmp, fin);
				close(lfd);
			}
			w->live_last_ts = f->ts_us;
		}

		if (w->vid_fd < 0) {
			w->written_frames++;           /* --preview: nothing to write */
			w->written_bytes += f->len;
		} else if (w->error) {
			w->discarded++;                /* output failed: drain, do not write */
		} else {
			t0 = now_mono_us();
			if (writer_put(w, f) == 0) {
				w->written_frames++;
				w->written_bytes += f->len;
			} else {
				w->discarded++;
			}
			t1 = now_mono_us();
			if ((double)(t1 - t0) / 1000.0 > w->write_max_ms)
				w->write_max_ms = (double)(t1 - t0) / 1000.0;
		}
		free(f->data);
		free(f);

		/* Steady writeback: kick each chunk out to the card as soon as it
		 * is complete, check the one before it, and drop that from the page
		 * cache, so dirty data never piles up into a multi-hundred-ms flush
		 * and a failing card is noticed within one chunk. */
		now = now_mono_us();
		if (w->vid_fd >= 0 && !w->error &&
		    (w->wb[0].size - w->wb[0].kicked >= w->wb_chunk || now - w->wb_last_us >= WB_MAX_INTERVAL_US))
			writer_writeback(w, now);
		writer_housekeeping(w, now);
	}
	if (w->vid_fd >= 0 && !w->error) {
		static const char *const what[3] = {"fdatasync .mjpeg", "fdatasync _timestamps.bin",
						    "fdatasync _index.bin"};
		for (i = 0; i < 3 && !w->error; i++)
			if (fdatasync(w->wb[i].fd))
				writer_fail(w, errno, what[i]);
	}
	return NULL;
}

/* ---- capture side ------------------------------------------------------- */

struct capture {
	const struct cfg *c;
	struct queue *q;
	int64_t msg_last_us;         /* rate limit for per-frame messages */
	unsigned int msg_suppressed;
};

/*
 * Per-frame messages (gaps, queue full, timestamp going backwards) from the
 * capture thread: at most one a second, so a burst of gaps cannot block the
 * capture thread on the journal socket. Every gap is still in gap_list.
 */
static void capture_msg(struct capture *cp, int64_t now, const char *fmt, ...)
	__attribute__((format(printf, 3, 4)));
static void capture_msg(struct capture *cp, int64_t now, const char *fmt, ...)
{
	va_list ap;

	if (cp->c->quiet)
		return;
	if (cp->msg_last_us && now - cp->msg_last_us < 1000000) {
		cp->msg_suppressed++;
		return;
	}
	va_start(ap, fmt);
	vfprintf(stderr, fmt, ap);
	va_end(ap);
	if (cp->msg_suppressed)
		fprintf(stderr, " (+%u more messages in the last second)", cp->msg_suppressed);
	fputc('\n', stderr);
	cp->msg_suppressed = 0;
	cp->msg_last_us = now;
}

/*
 * Account for one frame and hand a copy to the writer. data/len is the frame
 * (len 0 = an empty or invalid buffer: counted for sequence and timing only).
 * Statistics live in the queue and are updated under its mutex, so the writer
 * thread can snapshot them; no I/O here apart from rate-limited messages.
 */
static void capture_frame(struct capture *cp, const void *data, size_t len, int64_t ts_us,
			  uint32_t seq, uint32_t flags, int64_t now)
{
	struct queue *q = cp->q;
	struct stats *st = &q->st;
	struct frame *f = NULL;
	uint32_t prev_seq = 0, missing = 0;
	int64_t prev_ts = 0;
	int gap = 0, backwards = 0, overrun = 0;

	pthread_mutex_lock(&q->m);
	q->tstamp_flags = flags;
	if (!st->have_seq) {
		st->first_seq = seq;
		st->first_ts = ts_us;
		st->have_seq = 1;
	} else {
		if (seq != st->last_seq + 1) {
			missing = seq - st->last_seq - 1;
			prev_seq = st->last_seq;
			gap = 1;
			st->gaps++;
			st->lost += missing;
			if (strlen(st->gap_list) < sizeof(st->gap_list) - 32) {
				char g[32];
				snprintf(g, sizeof(g), "%s%u->%u", st->gap_list[0] ? "," : "", st->last_seq, seq);
				strncat(st->gap_list, g, sizeof(st->gap_list) - strlen(st->gap_list) - 1);
			}
		}
		if (ts_us > st->last_ts) {
			stats_add_interval(st, (double)(ts_us - st->last_ts));
		} else {
			backwards = 1;
			prev_ts = st->last_ts;
		}
	}
	st->last_seq = seq;
	st->last_ts = ts_us;
	if (flags & V4L2_BUF_FLAG_ERROR)
		st->errors++;
	if (len > 0) {
		st->frames++;
		st->bytes += len;
		st->i_bytes += len;
		if (q->bytes + len > q->max_bytes) {
			q->overruns++;
			overrun = 1;
		}
	}
	pthread_mutex_unlock(&q->m);

	if (gap)
		capture_msg(cp, now, "gap: seq %u -> %u (%u lost)", prev_seq, seq, missing);
	if (backwards)
		capture_msg(cp, now, "warning: timestamp went backwards (%" PRId64 " -> %" PRId64 " us)",
			    prev_ts, ts_us);
	if (len == 0)
		return;

	if (!overrun) {
		f = malloc(sizeof(*f));
		if (f) {
			f->data = malloc(len);
			if (!f->data) {
				free(f);
				f = NULL;
			}
		}
		if (!f) {
			overrun = 1;
			pthread_mutex_lock(&q->m);
			q->overruns++;
			pthread_mutex_unlock(&q->m);
		}
	}
	if (!f) {
		capture_msg(cp, now, "writer queue full: frame seq %u not written", seq);
		return;
	}
	memcpy(f->data, data, len);
	f->len = len;
	f->ts_us = ts_us;
	f->seq = seq;
	f->flags = flags;
	f->next = NULL;
	pthread_mutex_lock(&q->m);
	if (q->tail)
		q->tail->next = f;
	else
		q->head = f;
	q->tail = f;
	q->bytes += f->len;
	q->frames++;
	if (q->bytes > q->high_water)
		q->high_water = q->bytes;
	pthread_cond_signal(&q->cv);
	pthread_mutex_unlock(&q->m);
}

/* A USB camera as seen in /dev/v4l/by-path: its hub port spec and the link. */
struct cam_entry {
	char port[64];
	char path[PATH_MAX];
};
#define MAX_CAMS 16

/* port spec from a by-path name: ...-usb-0:1.3:1.0-video-index0 -> "1.3" */
static void port_of(const char *bypath, char *port, size_t len)
{
	const char *p = strstr(bypath, "-usb-");
	const char *a = p ? strchr(p + 5, ':') : NULL;
	const char *b = a ? strstr(a + 1, "-video") : NULL;

	snprintf(port, len, "?");
	if (a && b) {
		const char *c = b;
		while (c > a && *c != ':')
			c--;
		if (c > a + 1)
			snprintf(port, len, "%.*s", (int)(c - a - 1), a + 1);
	}
}

static int cmp_cam(const void *x, const void *y)
{
	return strverscmp(((const struct cam_entry *)x)->port, ((const struct cam_entry *)y)->port);
}

/*
 * All USB cameras ordered by hub port (1.1 before 1.3 before 1.10), from the
 * -usb- by-path links only: udev also makes -usbv2- links for the same nodes.
 */
static int enumerate_cameras(struct cam_entry *cams, int max)
{
	glob_t g;
	size_t i;
	int n = 0;

	/* zero the padding after the strings: resolve_device() memcmp()s entries */
	memset(cams, 0, sizeof(*cams) * max);
	if (glob("/dev/v4l/by-path/*-usb-*-video-index0", 0, NULL, &g) != 0)
		return 0;
	for (i = 0; i < g.gl_pathc && n < max; i++) {
		port_of(g.gl_pathv[i], cams[n].port, sizeof(cams[n].port));
		snprintf(cams[n].path, sizeof(cams[n].path), "%s", g.gl_pathv[i]);
		n++;
	}
	globfree(&g);
	qsort(cams, n, sizeof(cams[0]), cmp_cam);
	return n;
}

/* strlcpy */
static void copy_str(char *dst, size_t len, const char *src)
{
	size_t n = strlen(src);
	if (n >= len)
		n = len - 1;
	memcpy(dst, src, n);
	dst[n] = 0;
}

/*
 * Port claims: every instance takes an flock on <lock dir>/port-<port>.lock
 * for the camera it records (released by the kernel when the process exits,
 * however it exits), and auto: never picks a port another instance holds.
 * Without this, camera1's fallback after --wait could take the camera that
 * camera2 resolves a moment later, and one of them would loop on EBUSY.
 */
static const char *g_lock_dir = "/run/ewego";
static int g_lock_fd = -1;

/* 0 = claimed (or locking unavailable), -1 = another process holds the port */
static int claim_port(const char *port)
{
	static int warned;
	char path[PATH_MAX];
	int fd;

	if (!g_lock_dir || !g_lock_dir[0] || !port[0] || strcmp(port, "?") == 0)
		return 0;
	if (mkdir_p(g_lock_dir, 0755) == 0) {
		snprintf(path, sizeof(path), "%s/port-%s.lock", g_lock_dir, port);
		fd = open(path, O_RDWR | O_CREAT | O_CLOEXEC, 0644);
		if (fd >= 0) {
			if (flock(fd, LOCK_EX | LOCK_NB) == 0) {
				if (g_lock_fd >= 0)
					close(g_lock_fd);
				g_lock_fd = fd;
				return 0;
			}
			close(fd);
			if (errno == EWOULDBLOCK)
				return -1;
		}
	}
	if (!warned)
		fprintf(stderr, "note: cannot use port locks in %s (%s); not guarding against another instance\n",
			g_lock_dir, strerror(errno));
	warned = 1;
	return 0;
}

/*
 * Resolve --device:
 *   /dev/videoN, a by-path link   used as is
 *   port:1.3                      the camera on that hub port (stable for a
 *                                 physical port; /dev/videoN is not)
 *   auto:N[/M]                    the Nth camera (1-based) with the cameras
 *                                 ordered by hub port, so camera1 is the one
 *                                 on the lowest port whatever the hub or the
 *                                 enumeration order; M = how many cameras to
 *                                 expect (default N). The set has to be
 *                                 unchanged across a few polls first: after a
 *                                 hub reset the cameras come back one at a
 *                                 time and the order must not be decided
 *                                 while one is still missing.
 * final = the last try of the --wait loop: auto: then takes the Nth camera
 * even if fewer than M are present or the set is still changing, and if
 * another instance holds that one, any camera that no instance holds (a
 * camera recorded under the other name beats an EBUSY restart loop; the
 * summary's "port" says which physical camera it was).
 * The chosen port is claimed (see claim_port) before returning 0.
 * Returns 0 resolved, -1 not (yet) present, -2 bad spec, -3 auto: set
 * incomplete or still changing, -4 claimed by another instance.
 */
static int resolve_device(const char *spec, char *out, size_t outlen, char *port, size_t portlen, int final)
{
	struct cam_entry cams[MAX_CAMS];
	static struct cam_entry last[MAX_CAMS];
	static int last_n = -1, stable_polls;
	int n, i, k, found = 0;

	port[0] = 0;
	if (strncmp(spec, "auto:", 5) == 0) {
		char *end;
		int want = (int)strtol(spec + 5, &end, 10), expect = want;

		if (*end == '/')
			expect = (int)strtol(end + 1, &end, 10);
		if (*end || want < 1 || want > MAX_CAMS || expect < want || expect > MAX_CAMS) {
			fprintf(stderr, "ewego-cam: bad device spec %s (want auto:N or auto:N/M)\n", spec);
			return -2;
		}
		n = enumerate_cameras(cams, MAX_CAMS);
		if (n == last_n && memcmp(cams, last, sizeof(cams[0]) * n) == 0)
			stable_polls++;
		else
			stable_polls = 0;
		memcpy(last, cams, sizeof(cams[0]) * n);
		last_n = n;
		if (n < want)
			return -1;
		if (!final && (n < expect || stable_polls < 3))
			return -3;
		/* the Nth first; after the wait, then any other camera in port order */
		for (k = -1; k < n; k++) {
			i = k < 0 ? want - 1 : k;
			if (k == want - 1)
				continue;
			if (k >= 0 && !final)
				break;
			if (claim_port(cams[i].port) == 0) {
				snprintf(out, outlen, "%s", cams[i].path);
				copy_str(port, portlen, cams[i].port);
				if (k >= 0)
					fprintf(stderr, "ewego-cam: %s: port %s is held by another ewego-cam; "
						"recording the free camera on port %s instead\n",
						spec, cams[want - 1].port, cams[i].port);
				else if (n < expect || stable_polls < 3)
					fprintf(stderr, "ewego-cam: %s: %d of %d cameras present after the wait, using port %s\n",
						spec, n, expect, cams[i].port);
				return 0;
			}
		}
		return -4;
	}
	n = enumerate_cameras(cams, MAX_CAMS);
	if (strncmp(spec, "port:", 5) != 0) {
		char want_real[PATH_MAX], real[PATH_MAX];

		snprintf(out, outlen, "%s", spec);
		if (access(spec, F_OK) != 0)
			return -1;
		/* a node given by name: claim its port too, if it is a USB camera */
		if (realpath(spec, want_real))
			for (i = 0; i < n; i++)
				if (realpath(cams[i].path, real) && strcmp(real, want_real) == 0) {
					copy_str(port, portlen, cams[i].port);
					break;
				}
		return claim_port(port) == 0 ? 0 : -4;
	}
	for (i = 0; i < n; i++) {
		if (strcmp(cams[i].port, spec + 5) == 0) {
			snprintf(out, outlen, "%s", cams[i].path);
			copy_str(port, portlen, cams[i].port);
			found++;
		}
	}
	if (found > 1) {
		fprintf(stderr, "ewego-cam: %s matched %d cameras (want exactly 1); use --list\n", spec, found);
		return -2;
	}
	if (found == 0)
		return -1;
	return claim_port(port) == 0 ? 0 : -4;
}

static void list_cameras(void)
{
	struct cam_entry cams[MAX_CAMS];
	int n = enumerate_cameras(cams, MAX_CAMS), i;

	if (n == 0) {
		printf("no USB cameras\n");
		return;
	}
	for (i = 0; i < n; i++) {
		char real[PATH_MAX];
		if (!realpath(cams[i].path, real))
			snprintf(real, sizeof(real), "?");
		printf("auto:%-4d port:%-12s %s  (%s)\n", i + 1, cams[i].port, real, cams[i].path);
	}
}

static void usage(FILE *f)
{
	fprintf(f,
		"ewego-cam %s — record a UVC camera with kernel timestamps\n"
		"usage: ewego-cam --device DEV|port:SPEC|auto:N[/M] --out DIR [options]\n"
		"       ewego-cam --device ... --preview --live DIR     live files only, no recording\n"
		"  --device DEV        /dev/videoN, a by-path link, port:1.3 (USB hub port), or\n"
		"                      auto:N[/M] = Nth camera by hub port order (expecting M cameras)\n"
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
		"  --auto-fps          let auto-exposure lower the frame rate in dim light (default: hold the rate)\n"
		"  --wait N            wait up to N seconds for the camera to appear (default 30)\n"
		"  --live DIR          keep DIR/NAME.jpg (newest frame, <= 5/s) and DIR/NAME.json (counters, 1/s)\n"
		"  --preview           stream into the --live files only; nothing is recorded\n"
		"  --lock-dir DIR      port claims (port-<port>.lock) so two instances never take one camera\n"
		"                      (default /run/ewego; \"\" = off)\n"
		"  --quiet             no per-interval statistics on stdout\n"
		"  --list              list USB cameras and exit\n",
		VERSION, MAX_BUFFERS);
}

static int parse_size(const char *s, int *w, int *h)
{
	return sscanf(s, "%dx%d", w, h) == 2 && *w > 0 && *h > 0 ? 0 : -1;
}

/* O_EXCL: never truncate an existing recording. Returns fd, or -1 with errno. */
static int open_out(const char *dir, const char *name, const char *suffix)
{
	char path[PATH_MAX + 64];
	snprintf(path, sizeof(path), "%s/%s%s", dir, name, suffix);
	return open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0644);
}

/*
 * Create the session directory and the three output files, as late as
 * possible (after the camera is open and configured, so a failed start
 * leaves nothing behind). The stamp is local time to the second, so a
 * restart within the same second or after a clock step can land on an
 * existing session: the files are opened O_EXCL and, if they exist, the
 * session becomes <stamp>-2, <stamp>-3, ... With --no-session-dir an
 * existing recording is an error. Returns 0, or -1 (message printed).
 */
static int open_outputs(const struct cfg *c, char *dir, size_t dirlen, struct writer *w)
{
	static const char *const suffix[3] = {".mjpeg", "_timestamps.bin", "_index.bin"};
	char base[PATH_MAX];
	int k, i, fds[3];

	if (c->session_dir) {
		time_t t = time(NULL);
		struct tm tm;
		char stamp[32];
		localtime_r(&t, &tm);
		strftime(stamp, sizeof(stamp), "%Y%m%d_%H%M%S", &tm);
		snprintf(base, sizeof(base), "%s/%s", c->out, stamp);
	} else {
		snprintf(base, sizeof(base), "%s", c->out);
	}
	for (k = 1; k <= 99; k++) {
		if ((k == 1 ? snprintf(dir, dirlen, "%s", base)
			    : snprintf(dir, dirlen, "%s-%d", base, k)) >= (int)dirlen) {
			fprintf(stderr, "ewego-cam: %s: path too long\n", base);
			return -1;
		}
		if (mkdir_p(dir, 0755)) {
			fprintf(stderr, "ewego-cam: %s: %s\n", dir, strerror(errno));
			return -1;
		}
		for (i = 0; i < 3; i++) {
			fds[i] = open_out(dir, c->name, suffix[i]);
			if (fds[i] < 0)
				break;
		}
		if (i == 3) {
			w->vid_fd = fds[0];
			w->ts_fd = fds[1];
			w->idx_fd = fds[2];
			return 0;
		}
		{
			int err = errno;
			char path[PATH_MAX + 64];

			while (--i >= 0) {         /* undo the ones this attempt created */
				close(fds[i]);
				snprintf(path, sizeof(path), "%s/%s%s", dir, c->name, suffix[i]);
				unlink(path);
			}
			if (err != EEXIST || !c->session_dir) {
				fprintf(stderr, "ewego-cam: %s/%s.*: %s%s\n", dir, c->name, strerror(err),
					err == EEXIST ? " (not overwriting an earlier recording)" : "");
				return -1;
			}
		}
	}
	fprintf(stderr, "ewego-cam: %s: no free session name\n", base);
	return -1;
}

int main(int argc, char **argv)
{
	struct cfg c = {
		.device = NULL, .out = NULL, .name = "camera1",
		.width = 1920, .height = 1080, .fps = 30, .buffers = 16,
		.seconds = 0, .stats_interval = 5, .stall_timeout = 10,
		.ring_bytes = 64u << 20, .wb_chunk = 4u << 20,
		.session_dir = 1, .realtime = 0, .quiet = 0, .wait_secs = 30,
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
		{"auto-fps", no_argument, 0, 'A'}, {"wait", required_argument, 0, 'w'},
		{"live", required_argument, 0, 'L'}, {"preview", no_argument, 0, 'P'},
		{"quiet", no_argument, 0, 'q'},        {"list", no_argument, 0, 'l'},
		{"lock-dir", required_argument, 0, 'K'},
		{"help", no_argument, 0, 'h'},         {0, 0, 0, 0},
	};
	int opt;
	char devpath[PATH_MAX], port[64] = "";
	char dir[PATH_MAX], path[PATH_MAX + 64];
	int fd = -1;
	void *bufs[MAX_BUFFERS] = {0};
	size_t buflen[MAX_BUFFERS] = {0};
	struct queue q;
	struct writer w;
	struct capture cp;
	pthread_t writer_thread;
	pthread_condattr_t ca;
	int64_t mono0, real0, t_start, t_last_frame;
	int exit_code = 0;
	unsigned int i;

	memset(&q, 0, sizeof(q));
	memset(&w, 0, sizeof(w));
	memset(&cp, 0, sizeof(cp));
	setvbuf(stdout, NULL, _IOLBF, 0);   /* the stats line reaches the journal as it is printed */

	while ((opt = getopt_long(argc, argv, "d:o:n:s:f:b:R:W:t:S:y:x:NrAw:L:PqlhK:", longopts, NULL)) != -1) {
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
		case 'A': c.auto_fps = 1; break;
		case 'w': c.wait_secs = atoi(optarg); break;
		case 'L': c.live = optarg; break;
		case 'P': c.preview = 1; break;
		case 'q': c.quiet = 1; break;
		case 'K': g_lock_dir = optarg; break;
		case 'l': list_cameras(); return 0;
		case 'h': usage(stdout); return 0;
		default: usage(stderr); return 1;
		}
	}
	if (!c.device || (!c.out && !c.preview) || (c.preview && !c.live)) {
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

	/* Signals first: a stop during the device wait or the setup below must
	 * end the process cleanly (no SA_RESTART, so the waits return EINTR). */
	{
		struct sigaction sa;
		memset(&sa, 0, sizeof(sa));
		sa.sa_handler = on_signal;
		sigemptyset(&sa.sa_mask);
		sigaction(SIGINT, &sa, NULL);
		sigaction(SIGTERM, &sa, NULL);
		signal(SIGPIPE, SIG_IGN);
	}

	/* After a hub reset the camera re-enumerates a few seconds later; wait
	 * for it rather than failing the unit (systemd would retry anyway, but
	 * this keeps the gap and the log cleaner). */
	{
		int tries = 0, rc, limit = c.wait_secs * 2 > 1 ? c.wait_secs * 2 : 1;
		for (;;) {
			int final = tries + 1 >= limit;

			if (g_stop) {
				fprintf(stderr, "ewego-cam: stopped while waiting for %s; nothing recorded\n", c.device);
				return 0;
			}
			rc = resolve_device(c.device, devpath, sizeof(devpath), port, sizeof(port), final);
			if (rc == 0)
				break;
			if (rc == -2)
				return 1;
			if (final) {
				if (rc == -4)
					fprintf(stderr, "ewego-cam: camera for %s is held by another ewego-cam (port lock in %s)\n",
						c.device, g_lock_dir);
				else
					fprintf(stderr, "ewego-cam: no camera for %s after %d s\n", c.device, c.wait_secs);
				return 1;
			}
			if (++tries == 1)
				fprintf(stderr, "ewego-cam: waiting up to %d s for %s ...\n", c.wait_secs, c.device);
			usleep(500000);
		}
		if (strncmp(c.device, "auto:", 5) == 0)
			fprintf(stderr, "ewego-cam: %s -> %s (port %s)\n", c.device, devpath, port[0] ? port : "?");
	}

	dir[0] = 0;
	if (c.live && mkdir_p(c.live, 0755)) {
		perror(c.live);
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
	if (!c.auto_fps) {
		/* UVC "exposure, auto priority": when set, auto-exposure may lengthen
		 * the exposure beyond the frame period and the camera delivers fewer
		 * frames in dim light (seen: 24 fps in the evening). Clear it so the
		 * rate stays fixed and frames get darker instead. */
		struct v4l2_control ctl = {.id = V4L2_CID_EXPOSURE_AUTO_PRIORITY, .value = 0};
		if (xioctl(fd, VIDIOC_S_CTRL, &ctl))
			fprintf(stderr, "note: could not clear exposure-auto-priority (%s); frame rate may drop in dim light\n",
				strerror(errno));
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

	{
		enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
		if (xioctl(fd, VIDIOC_STREAMON, &type)) {
			perror("VIDIOC_STREAMON");
			return 1;
		}
	}

	/* output files and writer thread: only now, with the camera streaming
	 * (the queued buffers hold the first frames meanwhile), so a start that
	 * fails or is stopped earlier leaves no empty session behind */
	if (g_stop) {
		fprintf(stderr, "ewego-cam: stopped during setup; nothing recorded\n");
		return 0;
	}
	w.vid_fd = w.ts_fd = w.idx_fd = -1;
	if (!c.preview && open_outputs(&c, dir, sizeof(dir), &w))
		return 4;
	w.wb[0].fd = w.vid_fd;
	w.wb[1].fd = w.ts_fd;
	w.wb[2].fd = w.idx_fd;
	w.wb_last_us = now_mono_us();
	w.live = c.live;
	w.name = c.name;
	w.live_last_ts = -1;   /* not INT64_MIN: ts - INT64_MIN overflows and the frame is never written */
	pthread_mutex_init(&q.m, NULL);
	pthread_condattr_init(&ca);
	pthread_condattr_setclock(&ca, CLOCK_MONOTONIC);
	pthread_cond_init(&q.cv, &ca);
	q.max_bytes = c.ring_bytes;
	w.q = &q;
	w.wb_chunk = c.wb_chunk;
	w.c = &c;
	w.devpath = devpath;
	w.dir = c.preview ? NULL : dir;
	cp.c = &c;
	cp.q = &q;
	w.port = port;
	/* the wall-clock mapping, taken as the stream starts */
	mono0 = now_mono_us();
	real0 = now_real_us();
	w.t_start = w.t_last_stats = w.t_last_live = mono0;
	w.mono0 = mono0;
	w.real0 = real0;
	/* before --rt: the writer must not inherit SCHED_FIFO; and signals are
	 * left to the main thread */
	{
		sigset_t ss, old;
		int rc;
		sigemptyset(&ss);
		sigaddset(&ss, SIGINT);
		sigaddset(&ss, SIGTERM);
		pthread_sigmask(SIG_BLOCK, &ss, &old);
		rc = pthread_create(&writer_thread, NULL, writer_main, &w);
		pthread_sigmask(SIG_SETMASK, &old, NULL);
		if (rc) {
			fprintf(stderr, "pthread_create: %s\n", strerror(rc));
			return 1;
		}
	}

	if (c.realtime) {
		struct sched_param sp = {.sched_priority = 10};
		if (sched_setscheduler(0, SCHED_FIFO, &sp))
			fprintf(stderr, "note: SCHED_FIFO not granted (%s)\n", strerror(errno));
		if (mlockall(MCL_CURRENT | MCL_FUTURE))
			fprintf(stderr, "note: mlockall failed (%s)\n", strerror(errno));
	}

	t_start = t_last_frame = now_mono_us();
	if (c.preview)
		fprintf(stderr, "preview %dx%d MJPG @ %d fps -> %s/%s.jpg (nothing recorded)\n",
			c.width, c.height, c.fps, c.live, c.name);
	else
		fprintf(stderr, "recording %dx%d MJPG @ %d fps, %d buffers, %zu MB queue -> %s/%s.*%s%s\n",
			c.width, c.height, c.fps, c.buffers, c.ring_bytes >> 20, dir, c.name,
			c.live ? ", live files in " : "", c.live ? c.live : "");
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
				pthread_mutex_lock(&q.m);
				q.st.stalls++;
				pthread_mutex_unlock(&q.m);
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
			t_last_frame = now;
			if (b.bytesused > 0 && b.bytesused <= buflen[b.index])
				capture_frame(&cp, bufs[b.index], b.bytesused, ts_us, b.sequence, b.flags, now);
			else
				capture_frame(&cp, NULL, 0, ts_us, b.sequence, b.flags, now);
			if (xioctl(fd, VIDIOC_QBUF, &b)) {
				perror("VIDIOC_QBUF");
				exit_code = 1;
				break;
			}
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
	if (w.vid_fd >= 0) {
		close(w.vid_fd);
		close(w.ts_fd);
		close(w.idx_fd);
	}
	if (c.live) {
		snprintf(path, sizeof(path), "%s/%s.jpg", c.live, c.name);
		unlink(path);
		snprintf(path, sizeof(path), "%s/%s.json", c.live, c.name);
		unlink(path);
	}

	{
		struct snapshot s;
		const struct stats *st = &s.st;

		take_snapshot(&q, &s, 0);
		if (w.error)
			exit_code = 4;
		else if (exit_code == 0 && (st->lost || st->errors || s.qovr))
			exit_code = 2;
		if (!c.preview && write_summary(&c, &s, &w, now_mono_us(), now_real_us(), exit_code, 0))
			fprintf(stderr, "ewego-cam: could not write %s/%s_summary.json: %s\n", dir, c.name, strerror(errno));
		fprintf(stderr, "%s: %" PRIu64 " frames, %" PRIu64 " lost in %" PRIu64 " gap(s), %" PRIu64 " error-flagged, "
		"%" PRIu64 " queue overruns, %.1f MB, interval min/mean/max %.1f/%.1f/%.1f ms, "
		"queue peak %.1f MB, write max %.0f ms -> exit %d\n",
		c.name, st->frames, st->lost, st->gaps, st->errors, s.qovr, (double)w.written_bytes / 1e6,
		st->dt_n ? st->dt_min / 1000.0 : 0.0, st->dt_n ? st->dt_sum / (double)st->dt_n / 1000.0 : 0.0,
		st->dt_n ? st->dt_max / 1000.0 : 0.0, (double)s.qhw / 1e6, w.write_max_ms, exit_code);
	}
	return exit_code;
}
