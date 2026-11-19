/*
 * ewego-rec — the recorder skeleton shared by ewego-imu-usb and ewego-imu-uart.
 *
 * A recorder is two threads. The reader (the program's main thread, --rt for
 * SCHED_FIFO) talks to the device, timestamps each sample and hands it to
 * rec_push(). The writer thread, run by this file, pops samples from a RAM
 * ring and does everything slow: the program's accounting callback, the CSV
 * row, the live JSON for the console, stats lines and the summary JSON.
 *
 * Samples are opaque fixed-size structs (rec_ops.sample_size); the program
 * supplies callbacks for what depends on their layout. Plain C, libc only.
 */
#ifndef EWEGO_REC_H
#define EWEGO_REC_H

#include <limits.h>
#include <pthread.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/types.h>

#define REC_RING_N 8192          /* samples between reader and writer: ~80 s at 100 Hz */
#define REC_LIVE_N 64            /* most recent samples kept for the live file */
#define REC_LIVE_INTERVAL_US 100000

/* exit codes shared by the recorders */
#define REC_EXIT_OK     0        /* every sample arrived, none flagged */
#define REC_EXIT_USAGE  1        /* usage, device or setup error */
#define REC_EXIT_LOSS   2        /* lost or flagged samples, ring overruns */
#define REC_EXIT_STALL  3        /* the device stopped answering (--stall) */
#define REC_EXIT_OUTPUT 4        /* the recording could not be written (ENOSPC, EIO, ...) */

#define REC_FLUSH_US 1000000     /* CSV stdio buffer -> kernel */
#define REC_SYNC_US 2000000      /* fdatasync: at most this much CSV is lost on a power cut */
#define REC_OUTPUT_FATAL_US 3000000  /* output failing this long stops the recorder (exit 4) */
#define REC_OUTPUT_CLEAN_US 10000000 /* ... and only this long without a failure is recovery */
#define REC_SUMMARY_US 5000000   /* in-progress summary when --stats is 0 */
#define REC_WATCHDOG_FRESH_US 10000000  /* WATCHDOG=1 only if a real write completed this recently */

/* set by SIGINT/SIGTERM (rec_signals) */
extern volatile sig_atomic_t rec_stop;

int64_t rec_mono_us(void);
int64_t rec_real_us(void);
static inline int16_t rec_le16(const uint8_t *p) { return (int16_t)(p[0] | (p[1] << 8)); }

/* mkdir -p: every component, EEXIST is fine. Returns 0 or -1 with errno. */
int rec_mkdir_p(const char *path, mode_t mode);

/* SIGINT and SIGTERM set rec_stop (no SA_RESTART, so blocking calls return
 * EINTR). Install before any wait for the device. */
void rec_signals(void);

/* sd_notify(3) without libsystemd: one datagram to $NOTIFY_SOCKET, if set */
void rec_notify(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

/* BNO055 register burst 0x08..0x35 (46 bytes), shared by both IMU recorders */
#define REC_BNO_BURST_LEN 46
/* "<local time>.<us>" then the 27 sensor columns of log_imu_data.py (empty
 * when burst is NULL); no newline */
void rec_bno_csv(FILE *f, int64_t host_real_us, const uint8_t *burst);
/* ",heading,roll,pitch,ax,ay,az,gx,gy,gz,lax,lay,laz" for the live rows
 * (nulls when burst is NULL) */
void rec_bno_live(FILE *f, const uint8_t *burst);

struct rec;

/* ring counters copied under the ring mutex */
struct rec_snap {
	uint64_t overruns;
	unsigned peak;
};

struct rec_ops {
	size_t sample_size;
	const char *csv_header;          /* full first line, with the newline */
	const char *live_cols;           /* JSON array text for the live "cols" key */
	/* writer thread, before the row is written: counters for one sample */
	void (*account)(struct rec *r, const void *sample);
	/* one CSV line, with the newline */
	void (*csv_row)(FILE *f, const void *sample);
	/* the sample's BNO055 burst if it carries valid sensor data, else NULL
	 * (live "temp_c"/"cal" come from the newest valid one) */
	const uint8_t *(*valid_burst)(const void *sample);
	/* live JSON: the program's counters, each "\"key\":value," */
	void (*live_counters)(FILE *f, struct rec *r, const struct rec_snap *q);
	/* one entry of the live "recent" array, "[n,...]" */
	void (*live_row)(FILE *f, uint64_t n, const void *sample);
	/* stats line after "[elapsed] " (no newline) */
	void (*stats)(char *buf, size_t len, struct rec *r, const struct rec_snap *q);
	/* summary JSON lines between "device" and "realtime_minus_monotonic_us",
	 * each "  \"key\": value,\n" */
	void (*summary)(FILE *f, struct rec *r, const struct rec_snap *q);
};

struct rec {
	/* set by the program before rec_start */
	const struct rec_ops *ops;
	void *app;                       /* the program's own state */
	const char *program;             /* "ewego-imu-usb 0.4.0" */
	const char *name, *device, *live_dir;
	int quiet, stats_interval, preview, realtime;

	/* filled by rec_open_output: session dir (empty in --preview) and CSV */
	char dir[PATH_MAX];
	FILE *csv;
	int dirfd;                       /* the session dir, for fsync after create/rename */

	int64_t t_start, real0;          /* rec_start: CLOCK_MONOTONIC and CLOCK_REALTIME at start */
	uint64_t n;                      /* samples processed by the writer */

	/* ring: reader -> writer */
	pthread_mutex_t m;
	pthread_cond_t cv;
	unsigned char *ring;
	unsigned head, tail, count, peak;
	uint64_t overruns;
	int done;

	/* output health (writer thread; final values read after rec_end) */
	uint64_t write_errors;           /* failed flush / fdatasync / summary writes */
	char write_error_msg[128];       /* the latest one, "" if none */
	int64_t werr_since;              /* start of the current failing stretch, 0 when healthy */
	int64_t werr_last;               /* latest failure */
	int output_failed;               /* failing for REC_OUTPUT_FATAL_US: exit 4 */
	double free_mb;                  /* statvfs of the session dir, -1 unknown */
	int64_t last_good_write;         /* last fdatasync that carried new rows (preview: live write) */
	uint64_t n_at_sync;

	/* writer-private */
	unsigned char *cur, *live;
	uint64_t live_n[REC_LIVE_N];
	unsigned live_head, live_count;
	pthread_t thread;
};

/* Session directory OUT/<YYYYmmdd_HHMMSS> (or OUT itself with
 * session_dir == 0), then OUT/.../<name>.csv with the header. A stamp that
 * already exists (restart within the same second, clock step) gets -2, -3,
 * ... appended; nothing earlier is ever truncated. Call it after the device
 * is open so a dead sensor leaves no empty directories. 0 or -1 (message
 * printed). */
int rec_open_output(struct rec *r, const char *out, int session_dir);

/* Allocate the ring, start the writer thread (SIGINT/SIGTERM blocked in
 * it), then (realtime) put the calling thread on SCHED_FIFO 10 and
 * mlockall, and tell systemd READY=1. 0 or -1. */
int rec_start(struct rec *r);

/* Reader: hand one sample to the writer (counts an overrun when full). */
void rec_push(struct rec *r, const void *sample);

/* Ring counters, copied under the mutex. */
struct rec_snap rec_snapshot(struct rec *r);

/* Stop the writer after it drained the ring, flush + fdatasync + close the
 * CSV, remove the live file. Returns REC_EXIT_OUTPUT if the recording
 * could not be written (now or earlier, persistently), else 0. */
int rec_end(struct rec *r);

/* Final <dir>/<name>_summary.json ("in_progress": false) with the exit
 * code (no-op in --preview). The writer thread writes the same file every
 * stats interval with "in_progress": true and "exit_code": null; each write
 * is tmp + fdatasync + rename, so a power cut leaves the previous one. */
void rec_write_summary(struct rec *r, int exit_code);

#endif
