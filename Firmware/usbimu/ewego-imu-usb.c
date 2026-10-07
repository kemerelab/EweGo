/*
 * ewego-imu-usb — log the hub_imu USB IMU (STM32F042 + BNO055, HID).
 *
 * Reads 64-byte HID input reports from /dev/hidrawN (see
 * Firmware/hub_imu/src/report.h), stamps each with CLOCK_MONOTONIC and
 * CLOCK_REALTIME on arrival, and writes:
 *
 *   <name>.csv            same columns as Firmware/IMU/log_imu_data.py
 *                         (timestamp, euler, quat, lin acc, gravity, acc, gyro,
 *                         mag, temp, calib) plus host_mono_us, dev_ts_us, seq,
 *                         i2c_us, flags
 *   <name>_summary.json   samples, lost (sequence gaps), flagged samples,
 *                         device-clock interval statistics, host-vs-device
 *                         clock drift, exit code
 *
 * Exit: 0 clean and nothing lost; 2 lost or flagged samples; 3 no reports
 * for --stall seconds; 1 usage/device error.
 *
 * Build: gcc -O2 -Wall -Wextra -static -o ewego-imu-usb ewego-imu-usb.c
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define VERSION "0.2"
#define USB_VID 0x1209
#define USB_PID 0x0001

#define RPT_LEN 64
#define RPT_FLAG_I2C_ERROR 0x01
#define RPT_FLAG_NO_SENSOR 0x02
#define RPT_FLAG_DROPPED   0x04

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

static void usage(FILE *f)
{
	fprintf(f, "ewego-imu-usb %s — log the hub_imu USB IMU\n"
		   "usage: ewego-imu-usb --out DIR [--device /dev/hidrawN] [--name imu] [--seconds N]\n"
		   "       [--stats N] [--stall N] [--no-session-dir] [--quiet] [--list]\n"
		   "  --device   default: the single hidraw node with VID:PID %04x:%04x\n",
		VERSION, USB_VID, USB_PID);
}

int main(int argc, char **argv)
{
	const char *dev = NULL, *out = NULL, *name = "imu";
	int seconds = 0, stats_interval = 5, stall = 5, session_dir = 1, quiet = 0;
	static const struct option lo[] = {
		{"device", 1, 0, 'd'}, {"out", 1, 0, 'o'}, {"name", 1, 0, 'n'}, {"seconds", 1, 0, 't'},
		{"stats", 1, 0, 'S'}, {"stall", 1, 0, 'x'}, {"no-session-dir", 0, 0, 'N'},
		{"quiet", 0, 0, 'q'}, {"list", 0, 0, 'l'}, {"help", 0, 0, 'h'}, {0, 0, 0, 0},
	};
	int opt;
	char devpath[PATH_MAX], dir[PATH_MAX], path[PATH_MAX + 64];

	while ((opt = getopt_long(argc, argv, "d:o:n:t:S:x:Nqlh", lo, NULL)) != -1) {
		switch (opt) {
		case 'd': dev = optarg; break;
		case 'o': out = optarg; break;
		case 'n': name = optarg; break;
		case 't': seconds = atoi(optarg); break;
		case 'S': stats_interval = atoi(optarg); break;
		case 'x': stall = atoi(optarg); break;
		case 'N': session_dir = 0; break;
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
	if (!out) {
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

	int fd = open(devpath, O_RDONLY | O_CLOEXEC);
	if (fd < 0) {
		fprintf(stderr, "open %s: %s\n", devpath, strerror(errno));
		return 1;
	}

	snprintf(path, sizeof(path), "%s/%s.csv", dir, name);
	FILE *csv = fopen(path, "w");
	if (!csv) {
		perror(path);
		return 1;
	}
	setvbuf(csv, NULL, _IOFBF, 64 * 1024);
	fputs("timestamp,heading_deg,roll_deg,pitch_deg,quat_w,quat_x,quat_y,quat_z,"
	      "lin_accel_x,lin_accel_y,lin_accel_z,gravity_x,gravity_y,gravity_z,"
	      "accel_x,accel_y,accel_z,gyro_x,gyro_y,gyro_z,mag_x,mag_y,mag_z,temp_c,"
	      "cal_sys,cal_gyro,cal_accel,cal_mag,host_mono_us,dev_ts_us,seq,i2c_us,flags\n", csv);

	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);

	uint64_t n = 0, lost = 0, gaps = 0, flagged = 0, i2c_err = 0, dropped = 0;
	uint16_t last_seq = 0;
	int have_seq = 0;
	uint32_t last_dev = 0;
	int64_t dev_total = 0;      /* accumulated device time, handles 32-bit wrap */
	int64_t first_dev = 0, first_host = 0, last_host = 0;
	double dt_min = 0, dt_max = 0, dt_sum = 0;
	uint64_t dt_n = 0;
	int64_t t_start = mono_us(), t_last_stats = t_start, t_last_rpt = t_start, t_last_flush = t_start;
	int64_t real0 = real_us();
	int exit_code = 0;
	uint32_t i2c_us_max = 0;

	fprintf(stderr, "ewego-imu-usb %s: %s -> %s/%s.csv\n", VERSION, devpath, dir, name);

	while (!g_stop) {
		struct pollfd pfd = {.fd = fd, .events = POLLIN};
		uint8_t b[RPT_LEN];
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
			ssize_t len = read(fd, b, sizeof(b));
			if (len < 0) {
				if (errno == EINTR)
					continue;
				fprintf(stderr, "read: %s (device unplugged?)\n", strerror(errno));
				exit_code = 3;
				break;
			}
			if (len != RPT_LEN || b[0] != 1) {
				if (!quiet)
					fprintf(stderr, "unexpected report: %zd bytes, version %u\n", len, b[0]);
				continue;
			}
			int64_t host_real = real_us();
			t_last_rpt = now;
			uint8_t flags = b[1];
			uint16_t seq = ule16(b + 2);
			uint32_t dev = ule32(b + 4);
			const uint8_t *bu = b + 8;
			uint16_t i2c_us = ule16(b + 54);

			if (!have_seq) {
				have_seq = 1;
				first_dev = dev;
				first_host = now;
				dev_total = 0;
			} else {
				uint16_t expect = (uint16_t)(last_seq + 1);
				if (seq != expect) {
					uint16_t missing = (uint16_t)(seq - expect);
					gaps++;
					lost += missing;
					if (!quiet)
						fprintf(stderr, "gap: seq %u -> %u (%u lost)\n", last_seq, seq, missing);
				}
				uint32_t ddt = dev - last_dev;      /* unsigned wrap-safe */
				dev_total += ddt;
				if (!(flags & RPT_FLAG_DROPPED) && seq == expect) {
					double d = ddt;
					if (dt_n == 0 || d < dt_min) dt_min = d;
					if (d > dt_max) dt_max = d;
					dt_sum += d;
					dt_n++;
				}
			}
			last_seq = seq;
			last_dev = dev;
			last_host = now;
			if (flags) {
				flagged++;
				if (flags & RPT_FLAG_I2C_ERROR) i2c_err++;
				if (flags & RPT_FLAG_DROPPED) dropped++;
			}
			if (i2c_us > i2c_us_max)
				i2c_us_max = i2c_us;

			/* CSV row (same columns and scaling as log_imu_data.py) */
			{
				time_t secs = (time_t)(host_real / 1000000);
				struct tm tm;
				char ts[40];
				localtime_r(&secs, &tm);
				strftime(ts, sizeof(ts), "%Y-%m-%dT%H:%M:%S", &tm);
				fprintf(csv, "%s.%06" PRId64, ts, host_real % 1000000);
			}
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
			fprintf(csv, ",%" PRId64 ",%" PRIu32 ",%u,%u,%u\n", now, dev, seq, i2c_us, flags);
			n++;
		}

		if (stats_interval > 0 && now - t_last_stats >= (int64_t)stats_interval * 1000000) {
			double rate = dt_n ? 1e6 / (dt_sum / (double)dt_n) : 0;
			/* device clock vs host clock: ppm drift so far */
			double drift_ppm = 0;
			if (have_seq && last_host > first_host)
				drift_ppm = ((double)dev_total - (double)(last_host - first_host)) / (double)(last_host - first_host) * 1e6;
			if (!quiet)
				printf("[%7.1fs] %s: %" PRIu64 " samples, %" PRIu64 " lost, %" PRIu64 " flagged | %.2f Hz (device clock), "
				       "interval %.0f/%.0f us, i2c max %u us, drift %+.0f ppm\n",
				       (double)(now - t_start) / 1e6, name, n, lost, flagged, rate,
				       dt_n ? dt_min : 0.0, dt_n ? dt_max : 0.0, i2c_us_max, drift_ppm);
			fflush(stdout);
			t_last_stats = now;
		}
		if (now - t_last_flush >= 1000000) {
			fflush(csv);
			t_last_flush = now;
		}
		if (seconds > 0 && now - t_start >= (int64_t)seconds * 1000000)
			break;
	}

	fclose(csv);
	close(fd);
	if (exit_code == 0 && (lost || flagged))
		exit_code = 2;

	snprintf(path, sizeof(path), "%s/%s_summary.json", dir, name);
	FILE *js = fopen(path, "w");
	if (js) {
		double drift_ppm = (have_seq && last_host > first_host)
			? ((double)dev_total - (double)(last_host - first_host)) / (double)(last_host - first_host) * 1e6 : 0;
		fprintf(js, "{\n  \"program\": \"ewego-imu-usb %s\",\n  \"device\": \"%s\",\n  \"samples\": %" PRIu64 ",\n"
			    "  \"lost\": %" PRIu64 ",\n  \"gaps\": %" PRIu64 ",\n  \"flagged\": %" PRIu64 ",\n"
			    "  \"i2c_errors\": %" PRIu64 ",\n  \"host_not_polling\": %" PRIu64 ",\n"
			    "  \"interval_us\": {\"min\": %.0f, \"mean\": %.1f, \"max\": %.0f, \"n\": %" PRIu64 "},\n"
			    "  \"effective_hz\": %.3f,\n  \"i2c_us_max\": %u,\n  \"device_minus_host_drift_ppm\": %.1f,\n"
			    "  \"first_dev_ts_us\": %" PRId64 ",\n  \"first_host_mono_us\": %" PRId64 ",\n"
			    "  \"realtime_minus_monotonic_us\": %" PRId64 ",\n  \"exit_code\": %d\n}\n",
			VERSION, devpath, n, lost, gaps, flagged, i2c_err, dropped,
			dt_n ? dt_min : 0.0, dt_n ? dt_sum / (double)dt_n : 0.0, dt_n ? dt_max : 0.0, dt_n,
			dt_n ? 1e6 / (dt_sum / (double)dt_n) : 0.0, i2c_us_max, drift_ppm,
			first_dev, first_host, real0 - t_start, exit_code);
		fclose(js);
	}
	fprintf(stderr, "%s: %" PRIu64 " samples, %" PRIu64 " lost in %" PRIu64 " gap(s), %" PRIu64 " flagged -> exit %d\n",
		name, n, lost, gaps, flagged, exit_code);
	return exit_code;
}
