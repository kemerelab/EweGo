/*
 * hub_imu HID input report: one BNO055 sample per report, 64 bytes,
 * little-endian. Shared by the firmware and the host logger.
 *
 *  off  size  field
 *   0    1    version        REPORT_VERSION (1)
 *   1    1    flags          bit0 I2C error on this sample (burst invalid)
 *                            bit1 sensor not initialised
 *                            bit2 previous report was dropped (host not polling)
 *   2    2    seq            sample counter, wraps at 65535
 *   4    4    dev_ts_us      device microsecond clock at the start of the I2C burst
 *   8   46    burst          BNO055 registers 0x08..0x35: acc, mag, gyr, euler,
 *                            quat, lin acc, gravity, temp, calib (same layout
 *                            as the chip)
 *  54    2    i2c_us         how long the burst read took, microseconds
 *  56    2    period_us      configured sample period
 *  58    6    reserved       0
 */
#ifndef HUBIMU_REPORT_H
#define HUBIMU_REPORT_H
#include <stdint.h>

#define REPORT_VERSION   1
#define REPORT_LEN       64
#define REPORT_BURST_LEN 46

#define RPT_FLAG_I2C_ERROR  0x01
#define RPT_FLAG_NO_SENSOR  0x02
#define RPT_FLAG_DROPPED    0x04

struct __attribute__((packed)) imu_report {
	uint8_t version;
	uint8_t flags;
	uint16_t seq;
	uint32_t dev_ts_us;
	uint8_t burst[REPORT_BURST_LEN];
	uint16_t i2c_us;
	uint16_t period_us;
	uint8_t reserved[6];
};

#endif
