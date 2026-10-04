#ifndef HUBIMU_BNO055_H
#define HUBIMU_BNO055_H
#include <stdint.h>

#define BNO055_ADDR 0x28

/* registers, page 0 */
#define BNO_CHIP_ID    0x00   /* 0xA0 */
#define BNO_ACC_ID     0x01
#define BNO_MAG_ID     0x02
#define BNO_GYR_ID     0x03
#define BNO_SW_REV_LSB 0x04
#define BNO_BL_REV     0x06
#define BNO_PAGE_ID    0x07
#define BNO_ACC_DATA   0x08   /* start of the data block */
#define BNO_TEMP       0x34
#define BNO_CALIB_STAT 0x35
#define BNO_SYS_STATUS 0x39
#define BNO_SYS_ERR    0x3A
#define BNO_UNIT_SEL   0x3B
#define BNO_OPR_MODE   0x3D
#define BNO_PWR_MODE   0x3E
#define BNO_SYS_TRIGGER 0x3F

#define BNO_MODE_CONFIG 0x00
#define BNO_MODE_NDOF   0x0C

/* 0x08..0x35 inclusive: acc, mag, gyr, euler, quat, lia, grv, temp, calib */
#define BNO_BURST_LEN 46

struct bno055_info {
	uint8_t chip_id, acc_id, mag_id, gyr_id;
	uint16_t sw_rev;
	uint8_t bl_rev, sys_status, sys_err, opr_mode;
};

/* Pulse nRESET (PA6), wait for boot, verify the chip ID, enter NDOF.
 * Returns 0 on success, negative on I2C error, 1 on wrong chip ID. */
int bno055_init(void);
int bno055_info(struct bno055_info *info);
/* Read the 46-byte data block 0x08..0x35 into buf. */
int bno055_read_burst(uint8_t *buf);
int bno055_calib(uint8_t *calib);

#endif
