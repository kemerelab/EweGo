/*
 * ewego-rec — recorder skeleton shared by the IMU recorders; see ewego-rec.h.
 */
#define _GNU_SOURCE
#include "ewego-rec.h"

#include <errno.h>
#include <inttypes.h>
#include <sched.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
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

	if (session_dir) {
		time_t t = time(NULL);
		struct tm tm;
		char stamp[32];
		localtime_r(&t, &tm);
		strftime(stamp, sizeof(stamp), "%Y%m%d_%H%M%S", &tm);
		snprintf(r->dir, sizeof(r->dir), "%s/%s", out, stamp);
	} else {
		snprintf(r->dir, sizeof(r->dir), "%s", out);
	}
	if (rec_mkdir_p(r->dir, 0755)) {
		perror(r->dir);
		return -1;
	}
	snprintf(path, sizeof(path), "%s/%s.csv", r->dir, r->name);
	r->csv = fopen(path, "w");
	if (!r->csv) {
		perror(path);
		return -1;
	}
	setvbuf(r->csv, NULL, _IOFBF, 64 * 1024);
	fputs(r->ops->csv_header, r->csv);
	return 0;
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

static void write_live(struct rec *r, int64_t now)
{
	char tmp[PATH_MAX + 64], fin[PATH_MAX + 64];
	size_t sz = r->ops->sample_size;
	struct rec_snap q = rec_snapshot(r);
	const uint8_t *latest = NULL;
	unsigned i;
	FILE *f;

	snprintf(fin, sizeof(fin), "%s/%s.json", r->live_dir, r->name);
	snprintf(tmp, sizeof(tmp), "%s/.%s.json.tmp", r->live_dir, r->name);
	f = fopen(tmp, "w");
	if (!f)
		return;
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
	fclose(f);
	rename(tmp, fin);
}

static void print_stats(struct rec *r, int64_t now)
{
	struct rec_snap q = rec_snapshot(r);
	char line[512];

	r->ops->stats(line, sizeof(line), r, &q);
	printf("[%7.1fs] %s\n", (double)(now - r->t_start) / 1e6, line);
	fflush(stdout);
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
	int64_t t_last_stats = r->t_start, t_last_flush = r->t_start, t_last_live = r->t_start;

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
		if (r->stats_interval > 0 && !r->quiet && now - t_last_stats >= (int64_t)r->stats_interval * 1000000) {
			print_stats(r, now);
			t_last_stats = now;
		}
		if (r->csv && now - t_last_flush >= 1000000) {
			fflush(r->csv);
			t_last_flush = now;
		}
		if (r->live_dir && now - t_last_live >= REC_LIVE_INTERVAL_US) {
			write_live(r, now);
			t_last_live = now;
		}
	}
	return NULL;
}

int rec_start(struct rec *r)
{
	size_t sz = r->ops->sample_size;
	pthread_condattr_t ca;

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
	if (pthread_create(&r->thread, NULL, writer_main, r)) {
		perror("pthread_create");
		return -1;
	}
	/* after the writer exists, so it keeps the normal scheduling class */
	if (r->realtime) {
		struct sched_param sp = {.sched_priority = 10};
		if (sched_setscheduler(0, SCHED_FIFO, &sp))
			fprintf(stderr, "note: SCHED_FIFO not granted (%s)\n", strerror(errno));
		if (mlockall(MCL_CURRENT | MCL_FUTURE))
			fprintf(stderr, "note: mlockall failed (%s)\n", strerror(errno));
	}
	return 0;
}

void rec_end(struct rec *r)
{
	char path[PATH_MAX + 64];

	pthread_mutex_lock(&r->m);
	r->done = 1;
	pthread_cond_signal(&r->cv);
	pthread_mutex_unlock(&r->m);
	pthread_join(r->thread, NULL);
	if (r->csv) {
		fclose(r->csv);
		r->csv = NULL;
	}
	if (r->live_dir) {
		snprintf(path, sizeof(path), "%s/%s.json", r->live_dir, r->name);
		unlink(path);
	}
}

void rec_write_summary(struct rec *r, int exit_code)
{
	char path[PATH_MAX + 64];
	struct rec_snap q;
	FILE *js;

	if (r->preview)
		return;
	q = rec_snapshot(r);
	snprintf(path, sizeof(path), "%s/%s_summary.json", r->dir, r->name);
	js = fopen(path, "w");
	if (!js)
		return;
	fprintf(js, "{\n  \"program\": \"%s\",\n  \"device\": \"%s\",\n", r->program, r->device);
	r->ops->summary(js, r, &q);
	fprintf(js, "  \"realtime_minus_monotonic_us\": %" PRId64 ",\n  \"exit_code\": %d\n}\n", r->real0 - r->t_start,
		exit_code);
	fclose(js);
}
