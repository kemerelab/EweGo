/*
 * ewego-rec — recorder skeleton shared by the IMU recorders; see ewego-rec.h.
 */
#define _GNU_SOURCE
#include "ewego-rec.h"

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <sched.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

volatile sig_atomic_t rec_stop;

static void on_signal(int s)
{
	(void)s;
	rec_stop = 1;
}

void rec_signals(void)
{
	struct sigaction sa;

	memset(&sa, 0, sizeof(sa));
	sa.sa_handler = on_signal;
	sigemptyset(&sa.sa_mask);
	sigaction(SIGINT, &sa, NULL);
	sigaction(SIGTERM, &sa, NULL);
}

void rec_notify(const char *fmt, ...)
{
	const char *path = getenv("NOTIFY_SOCKET");
	struct sockaddr_un addr;
	char msg[512];
	size_t len;
	va_list ap;
	int fd;

	if (!path || (path[0] != '/' && path[0] != '@'))
		return;
	len = strlen(path);
	if (len >= sizeof(addr.sun_path))
		return;
	va_start(ap, fmt);
	vsnprintf(msg, sizeof(msg), fmt, ap);
	va_end(ap);
	fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
	if (fd < 0)
		return;
	memset(&addr, 0, sizeof(addr));
	addr.sun_family = AF_UNIX;
	memcpy(addr.sun_path, path, len);
	if (addr.sun_path[0] == '@')         /* abstract namespace */
		addr.sun_path[0] = 0;
	sendto(fd, msg, strlen(msg), MSG_NOSIGNAL, (struct sockaddr *)&addr,
	       (socklen_t)(offsetof(struct sockaddr_un, sun_path) + len));
	close(fd);
}

int64_t rec_mono_us(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (int64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}

int64_t rec_real_us(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_REALTIME, &ts);
	return (int64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
}

int rec_mkdir_p(const char *path, mode_t mode)
{
	char buf[PATH_MAX];
	size_t len = strlen(path), i;

	if (len == 0 || len >= sizeof(buf)) {
		errno = ENAMETOOLONG;
		return -1;
	}
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

/* ---- BNO055 burst formatting -------------------------------------------- */

void rec_bno_csv(FILE *f, int64_t host_real_us, const uint8_t *bu)
{
	time_t secs = (time_t)(host_real_us / 1000000);
	struct tm tm;
	char ts[40];

	localtime_r(&secs, &tm);
	strftime(ts, sizeof(ts), "%Y-%m-%dT%H:%M:%S", &tm);
	fprintf(f, "%s.%06" PRId64, ts, host_real_us % 1000000);
	if (!bu) {
		fputs(",,,,,,,,,,,,,,,,,,,,,,,,,,,", f);
		return;
	}
	fprintf(f, ",%.4f,%.4f,%.4f", rec_le16(bu + 18) / 16.0, rec_le16(bu + 20) / 16.0, rec_le16(bu + 22) / 16.0);
	fprintf(f, ",%.6f,%.6f,%.6f,%.6f", rec_le16(bu + 24) / 16384.0, rec_le16(bu + 26) / 16384.0,
		rec_le16(bu + 28) / 16384.0, rec_le16(bu + 30) / 16384.0);
	fprintf(f, ",%.2f,%.2f,%.2f", rec_le16(bu + 32) / 100.0, rec_le16(bu + 34) / 100.0, rec_le16(bu + 36) / 100.0);
	fprintf(f, ",%.2f,%.2f,%.2f", rec_le16(bu + 38) / 100.0, rec_le16(bu + 40) / 100.0, rec_le16(bu + 42) / 100.0);
	fprintf(f, ",%.2f,%.2f,%.2f", rec_le16(bu + 0) / 100.0, rec_le16(bu + 2) / 100.0, rec_le16(bu + 4) / 100.0);
	fprintf(f, ",%.4f,%.4f,%.4f", rec_le16(bu + 12) / 16.0, rec_le16(bu + 14) / 16.0, rec_le16(bu + 16) / 16.0);
	fprintf(f, ",%.4f,%.4f,%.4f", rec_le16(bu + 6) / 16.0, rec_le16(bu + 8) / 16.0, rec_le16(bu + 10) / 16.0);
	fprintf(f, ",%d,%u,%u,%u,%u", (int8_t)bu[44], (bu[45] >> 6) & 3, (bu[45] >> 4) & 3, (bu[45] >> 2) & 3, bu[45] & 3);
}

void rec_bno_live(FILE *f, const uint8_t *bu)
{
	if (!bu) {
		fputs(",null,null,null,null,null,null,null,null,null,null,null,null", f);
		return;
	}
	fprintf(f, ",%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.3f,%.3f,%.3f,%.2f,%.2f,%.2f",
		rec_le16(bu + 18) / 16.0, rec_le16(bu + 20) / 16.0, rec_le16(bu + 22) / 16.0,
		rec_le16(bu + 0) / 100.0, rec_le16(bu + 2) / 100.0, rec_le16(bu + 4) / 100.0,
		rec_le16(bu + 12) / 16.0, rec_le16(bu + 14) / 16.0, rec_le16(bu + 16) / 16.0,
		rec_le16(bu + 32) / 100.0, rec_le16(bu + 34) / 100.0, rec_le16(bu + 36) / 100.0);
}

/* ---- output ------------------------------------------------------------- */

int rec_open_output(struct rec *r, const char *out, int session_dir)
{
	char path[PATH_MAX + 64];
	int k;

	r->dirfd = -1;
	r->free_mb = -1;
	if (rec_mkdir_p(out, 0755)) {
		perror(out);
		return -1;
	}
	if (session_dir) {
		time_t t = time(NULL);
		struct tm tm;
		char stamp[32];
		localtime_r(&t, &tm);
		strftime(stamp, sizeof(stamp), "%Y%m%d_%H%M%S", &tm);
		for (k = 1;; k++) {
			if (k == 1)
				snprintf(r->dir, sizeof(r->dir), "%s/%s", out, stamp);
			else
				snprintf(r->dir, sizeof(r->dir), "%s/%s-%d", out, stamp, k);
			if (mkdir(r->dir, 0755) == 0)
				break;
			if (errno != EEXIST || k >= 999) {
				perror(r->dir);
				return -1;
			}
		}
	} else {
		snprintf(r->dir, sizeof(r->dir), "%s", out);
	}
	snprintf(path, sizeof(path), "%s/%s.csv", r->dir, r->name);
	r->csv = fopen(path, "w");
	if (!r->csv) {
		perror(path);
		return -1;
	}
	setvbuf(r->csv, NULL, _IOFBF, 64 * 1024);
	fputs(r->ops->csv_header, r->csv);
	if (fflush(r->csv) || ferror(r->csv) || fdatasync(fileno(r->csv))) {
		perror(path);
		return -1;
	}
	/* make the new directory entries themselves survive a power cut */
	r->dirfd = open(r->dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (r->dirfd >= 0)
		fsync(r->dirfd);
	if (session_dir) {
		int ofd = open(out, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
		if (ofd >= 0) {
			fsync(ofd);
			close(ofd);
		}
	}
	return 0;
}

/* One output check: ok, or failed with err. A failure is counted and
 * shown at once (live JSON, stderr). Failures that keep coming for
 * REC_OUTPUT_FATAL_US stop the recorder with exit 4 rather than let it run
 * on writing nothing. stdio drops a buffer it failed to write, so the next
 * flush may "succeed" with nothing to write: only REC_OUTPUT_CLEAN_US of
 * checks without a failure count as recovered. */
static void output_check(struct rec *r, int ok, int err, const char *what, int64_t now)
{
	if (ok) {
		if (r->werr_since && !r->output_failed && now - r->werr_last >= REC_OUTPUT_CLEAN_US) {
			fprintf(stderr, "%s: output writable again\n", r->name);
			r->werr_since = 0;
		}
		return;
	}
	r->werr_last = now;
	r->write_errors++;
	snprintf(r->write_error_msg, sizeof(r->write_error_msg), "%s: %s", what, strerror(err ? err : EIO));
	if (!r->werr_since)
		r->werr_since = now;
	if (r->write_errors <= 10 || r->write_errors % 100 == 0)
		fprintf(stderr, "%s: output error #%" PRIu64 ": %s\n", r->name, r->write_errors, r->write_error_msg);
	if (!r->output_failed && now - r->werr_since >= REC_OUTPUT_FATAL_US) {
		r->output_failed = 1;
		fprintf(stderr, "%s: output failing for %.0f s (%s): stopping, exit %d\n", r->name,
			(double)(now - r->werr_since) / 1e6, r->write_error_msg, REC_EXIT_OUTPUT);
		rec_stop = 1;
	}
}

static void csv_flush(struct rec *r, int64_t now)
{
	int bad = fflush(r->csv) != 0 || ferror(r->csv);
	int err = errno;

	if (bad)
		clearerr(r->csv);
	output_check(r, !bad, err, "csv write", now);
}

static void csv_sync(struct rec *r, int64_t now)
{
	int bad = fdatasync(fileno(r->csv)) != 0;

	output_check(r, !bad, errno, "csv fdatasync", now);
	if (!bad && r->n > r->n_at_sync) {
		r->last_good_write = now;
		r->n_at_sync = r->n;
	}
}

static void update_free(struct rec *r)
{
	struct statvfs v;

	if (r->preview || statvfs(r->dir, &v))
		r->free_mb = -1;
	else
		r->free_mb = (double)v.f_bavail * (double)v.f_frsize / 1e6;
}

/* JSON string or null; the messages are our own (strerror), no escaping needed */
static void json_msg(FILE *f, const char *s)
{
	if (s[0])
		fprintf(f, "\"%s\"", s);
	else
		fputs("null", f);
}

/* ---- ring --------------------------------------------------------------- */

void rec_push(struct rec *r, const void *sample)
{
	size_t sz = r->ops->sample_size;

	pthread_mutex_lock(&r->m);
	if (r->count == REC_RING_N) {
		r->overruns++;
	} else {
		memcpy(r->ring + (size_t)r->head * sz, sample, sz);
		r->head = (r->head + 1) % REC_RING_N;
		r->count++;
		if (r->count > r->peak)
			r->peak = r->count;
	}
	pthread_cond_signal(&r->cv);
	pthread_mutex_unlock(&r->m);
}

struct rec_snap rec_snapshot(struct rec *r)
{
	struct rec_snap q;

	pthread_mutex_lock(&r->m);
	q.overruns = r->overruns;
	q.peak = r->peak;
	pthread_mutex_unlock(&r->m);
	return q;
}

/* ---- writer ------------------------------------------------------------- */

static int write_live(struct rec *r, int64_t now)
{
	char tmp[PATH_MAX + 64], fin[PATH_MAX + 64];
	size_t sz = r->ops->sample_size;
	struct rec_snap q = rec_snapshot(r);
	const uint8_t *latest = NULL;
	unsigned i;
	FILE *f;
	int bad;

	snprintf(fin, sizeof(fin), "%s/%s.json", r->live_dir, r->name);
	snprintf(tmp, sizeof(tmp), "%s/.%s.json.tmp", r->live_dir, r->name);
	f = fopen(tmp, "w");
	if (!f)
		return -1;
	/* newest sample with valid sensor data, for temperature and calibration */
	for (i = 0; i < r->live_count && !latest; i++)
		latest = r->ops->valid_burst(r->live + (size_t)((r->live_head + REC_LIVE_N - 1 - i) % REC_LIVE_N) * sz);
	fprintf(f, "{\"program\":\"%s\",\"name\":\"%s\",\"device\":\"%s\",\"preview\":%d,", r->program, r->name,
		r->device, r->preview);
	if (!r->preview)
		fprintf(f, "\"session\":\"%s\",", r->dir);
	else
		fputs("\"session\":null,", f);
	fprintf(f, "\"elapsed_s\":%.1f,", (double)(now - r->t_start) / 1e6);
	r->ops->live_counters(f, r, &q);
	fprintf(f, "\"writer_error\":%" PRIu64 ",\"writer_error_msg\":", r->write_errors);
	json_msg(f, r->write_error_msg);
	if (r->free_mb >= 0)
		fprintf(f, ",\"free_mb\":%.0f,", r->free_mb);
	else
		fputs(",\"free_mb\":null,", f);
	if (latest)
		fprintf(f, "\"temp_c\":%d,\"cal\":[%u,%u,%u,%u],", (int8_t)latest[44], (latest[45] >> 6) & 3,
			(latest[45] >> 4) & 3, (latest[45] >> 2) & 3, latest[45] & 3);
	else
		fputs("\"temp_c\":null,\"cal\":null,", f);
	fprintf(f, "\"updated_mono_us\":%" PRId64 ",\"updated_real_us\":%" PRId64 ",", now, rec_real_us());
	fprintf(f, "\"cols\":%s,\"recent\":[", r->ops->live_cols);
	for (i = 0; i < r->live_count; i++) {
		unsigned k = (r->live_head + REC_LIVE_N - r->live_count + i) % REC_LIVE_N;
		if (i)
			fputc(',', f);
		r->ops->live_row(f, r->live_n[k], r->live + (size_t)k * sz);
	}
	fputs("]}\n", f);
	bad = ferror(f);
	if (fclose(f) || bad || rename(tmp, fin)) {
		unlink(tmp);
		return -1;
	}
	return 0;
}

static void print_stats(struct rec *r, int64_t now)
{
	struct rec_snap q = rec_snapshot(r);
	char line[512], out[800];

	r->ops->stats(line, sizeof(line), r, &q);
	if (r->preview)
		snprintf(out, sizeof(out), "%s", line);
	else if (r->write_errors)
		snprintf(out, sizeof(out), "%s | free %.0f MB | WRITE ERRORS %" PRIu64 " (%s)", line, r->free_mb,
			 r->write_errors, r->write_error_msg);
	else
		snprintf(out, sizeof(out), "%s | free %.0f MB", line, r->free_mb);
	if (!r->quiet) {
		printf("[%7.1fs] %s\n", (double)(now - r->t_start) / 1e6, out);
		fflush(stdout);
	}
	rec_notify("STATUS=%s", out);
}

static void write_summary(struct rec *r, int exit_code, int in_progress)
{
	char tmp[PATH_MAX + 64], fin[PATH_MAX + 64];
	struct rec_snap q;
	int64_t now = rec_mono_us();
	FILE *js;
	int bad;

	if (r->preview)
		return;
	q = rec_snapshot(r);
	snprintf(fin, sizeof(fin), "%s/%s_summary.json", r->dir, r->name);
	snprintf(tmp, sizeof(tmp), "%s/.%s_summary.json.tmp", r->dir, r->name);
	js = fopen(tmp, "w");
	if (!js) {
		output_check(r, 0, errno, "summary", now);
		return;
	}
	fprintf(js, "{\n  \"program\": \"%s\",\n  \"device\": \"%s\",\n", r->program, r->device);
	r->ops->summary(js, r, &q);
	fprintf(js, "  \"realtime_minus_monotonic_us\": %" PRId64 ",\n  \"in_progress\": %s,\n"
		    "  \"writer_error\": %" PRIu64 ",\n  \"writer_error_msg\": ",
		r->real0 - r->t_start, in_progress ? "true" : "false", r->write_errors);
	json_msg(js, r->write_error_msg);
	if (r->free_mb >= 0)
		fprintf(js, ",\n  \"free_mb\": %.0f,\n", r->free_mb);
	else
		fputs(",\n  \"free_mb\": null,\n", js);
	if (in_progress)
		fputs("  \"exit_code\": null\n}\n", js);
	else
		fprintf(js, "  \"exit_code\": %d\n}\n", exit_code);
	bad = fflush(js) != 0 || ferror(js) || fdatasync(fileno(js)) != 0;
	if (fclose(js) || bad || rename(tmp, fin)) {
		output_check(r, 0, errno, "summary", now);
		unlink(tmp);
		return;
	}
	if (r->dirfd >= 0)
		fsync(r->dirfd);
}

static void process(struct rec *r)
{
	size_t sz = r->ops->sample_size;

	r->ops->account(r, r->cur);
	if (r->csv)
		r->ops->csv_row(r->csv, r->cur);
	if (r->live_dir) {
		memcpy(r->live + (size_t)r->live_head * sz, r->cur, sz);
		r->live_n[r->live_head] = r->n;
		r->live_head = (r->live_head + 1) % REC_LIVE_N;
		if (r->live_count < REC_LIVE_N)
			r->live_count++;
	}
	r->n++;
}

static void *writer_main(void *arg)
{
	struct rec *r = arg;
	size_t sz = r->ops->sample_size;
	int64_t t0 = r->t_start;
	int64_t t_stats = t0, t_flush = t0, t_sync = t0, t_live = t0, t_summary = t0, t_wd = t0;
	int64_t summary_us = r->stats_interval > 0 ? (int64_t)r->stats_interval * 1000000 : REC_SUMMARY_US;

	r->last_good_write = t0;
	update_free(r);
	for (;;) {
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
			memcpy(r->cur, r->ring + (size_t)r->tail * sz, sz);
			r->tail = (r->tail + 1) % REC_RING_N;
			r->count--;
			got = 1;
		}
		done = r->done;
		pthread_mutex_unlock(&r->m);

		if (got)
			process(r);
		else if (done)
			break;

		now = rec_mono_us();
		if (r->csv && now - t_flush >= REC_FLUSH_US) {
			csv_flush(r, now);
			update_free(r);
			t_flush = now;
		}
		/* the ring absorbs the SD card stall this may cause */
		if (r->csv && now - t_sync >= REC_SYNC_US) {
			csv_sync(r, rec_mono_us());
			t_sync = now;
		}
		if (r->stats_interval > 0 && now - t_stats >= (int64_t)r->stats_interval * 1000000) {
			print_stats(r, now);
			t_stats = now;
		}
		if (r->csv && now - t_summary >= summary_us) {
			write_summary(r, 0, 1);
			t_summary = now;
		}
		if (r->live_dir && now - t_live >= REC_LIVE_INTERVAL_US) {
			if (write_live(r, now) == 0 && r->preview)
				r->last_good_write = now;
			t_live = now;
		}
		/* systemd watchdog: only while writes really complete */
		if (now - t_wd >= 2000000 && now - r->last_good_write <= REC_WATCHDOG_FRESH_US && !r->output_failed) {
			rec_notify("WATCHDOG=1");
			t_wd = now;
		}
	}
	return NULL;
}

int rec_start(struct rec *r)
{
	size_t sz = r->ops->sample_size;
	pthread_condattr_t ca;
	sigset_t block, old;

	if (r->preview) {
		r->dirfd = -1;
		r->free_mb = -1;
	}
	r->ring = calloc(REC_RING_N, sz);
	r->live = calloc(REC_LIVE_N, sz);
	r->cur = calloc(1, sz);
	if (!r->ring || !r->live || !r->cur) {
		perror("calloc");
		return -1;
	}
	pthread_mutex_init(&r->m, NULL);
	/* the writer's timed wait uses CLOCK_MONOTONIC deadlines */
	pthread_condattr_init(&ca);
	pthread_condattr_setclock(&ca, CLOCK_MONOTONIC);
	pthread_cond_init(&r->cv, &ca);
	pthread_condattr_destroy(&ca);

	r->t_start = rec_mono_us();
	r->real0 = rec_real_us();
	/* signals go to the reader, whose poll/sleep they interrupt */
	sigemptyset(&block);
	sigaddset(&block, SIGINT);
	sigaddset(&block, SIGTERM);
	pthread_sigmask(SIG_BLOCK, &block, &old);
	if (pthread_create(&r->thread, NULL, writer_main, r)) {
		perror("pthread_create");
		return -1;
	}
	pthread_sigmask(SIG_SETMASK, &old, NULL);
	/* after the writer exists, so it keeps the normal scheduling class */
	if (r->realtime) {
		struct sched_param sp = {.sched_priority = 10};
		if (sched_setscheduler(0, SCHED_FIFO, &sp))
			fprintf(stderr, "note: SCHED_FIFO not granted (%s)\n", strerror(errno));
		if (mlockall(MCL_CURRENT | MCL_FUTURE))
			fprintf(stderr, "note: mlockall failed (%s)\n", strerror(errno));
	}
	rec_notify("READY=1\nSTATUS=%s %s", r->preview ? "preview of" : "recording", r->device);
	return 0;
}

int rec_end(struct rec *r)
{
	char path[PATH_MAX + 64];

	rec_notify("STOPPING=1");
	pthread_mutex_lock(&r->m);
	r->done = 1;
	pthread_cond_signal(&r->cv);
	pthread_mutex_unlock(&r->m);
	pthread_join(r->thread, NULL);
	if (r->csv) {
		int64_t now = rec_mono_us();
		int bad = fflush(r->csv) != 0 || ferror(r->csv);
		int err = errno;
		if (!bad && fdatasync(fileno(r->csv))) {
			bad = 1;
			err = errno;
		}
		if (fclose(r->csv) && !bad) {
			bad = 1;
			err = errno;
		}
		r->csv = NULL;
		if (bad) {
			/* the tail of the recording is gone: that is an output failure */
			output_check(r, 0, err, "csv close", now);
			r->output_failed = 1;
		}
		update_free(r);
	}
	if (r->live_dir) {
		snprintf(path, sizeof(path), "%s/%s.json", r->live_dir, r->name);
		unlink(path);
	}
	return r->output_failed ? REC_EXIT_OUTPUT : 0;
}

void rec_write_summary(struct rec *r, int exit_code)
{
	write_summary(r, exit_code, 0);
	if (r->dirfd >= 0) {
		close(r->dirfd);
		r->dirfd = -1;
	}
}
