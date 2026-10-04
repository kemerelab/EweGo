/* BNO055 over I2C, fusion mode (NDOF). Timing per Bosch BST-BNO055-DS000. */
#include "bno055.h"
#include "i2c.h"
#include "stm32f0xx.h"

extern volatile uint32_t g_ms;
/* Called while waiting (the sensor takes ~700 ms to boot); main() uses it
 * to keep servicing USB so enumeration is not stalled. */
extern void bno055_yield(void);

static void delay_ms(uint32_t ms)
{
	uint32_t t0 = g_ms;
	while (g_ms - t0 < ms)
		bno055_yield();
}

static void reset_pin_init(void)
{
	/* PA6 = nRESET, output, push-pull; board has a 10k pull-up */
	RCC->AHBENR |= RCC_AHBENR_GPIOAEN;
	GPIOA->BSRR = GPIO_BSRR_BS_6;
	GPIOA->MODER = (GPIOA->MODER & ~GPIO_MODER_MODER6) | GPIO_MODER_MODER6_0;
}

int bno055_init(void)
{
	uint8_t id = 0;
	int r;

	reset_pin_init();
	GPIOA->BSRR = GPIO_BSRR_BR_6;    /* hold in reset */
	delay_ms(10);
	GPIOA->BSRR = GPIO_BSRR_BS_6;
	/* datasheet: ~650 ms from reset to ready; poll the chip ID up to 1 s */
	for (uint32_t t0 = g_ms; g_ms - t0 < 1000;) {
		delay_ms(50);
		if (i2c_read_regs(BNO055_ADDR, BNO_CHIP_ID, &id, 1) == 0 && id == 0xA0)
			break;
	}
	if (id != 0xA0)
		return id == 0 ? -1 : 1;

	if ((r = i2c_write_reg(BNO055_ADDR, BNO_PAGE_ID, 0)))
		return r;
	if ((r = i2c_write_reg(BNO055_ADDR, BNO_OPR_MODE, BNO_MODE_CONFIG)))
		return r;
	delay_ms(25);
	if ((r = i2c_write_reg(BNO055_ADDR, BNO_PWR_MODE, 0x00)))   /* normal */
		return r;
	/* UNIT_SEL default: m/s^2, dps, degrees, Celsius, Windows orientation */
	if ((r = i2c_write_reg(BNO055_ADDR, BNO_UNIT_SEL, 0x00)))
		return r;
	/* internal oscillator (XIN32/XOUT32 are open on this board) */
	if ((r = i2c_write_reg(BNO055_ADDR, BNO_SYS_TRIGGER, 0x00)))
		return r;
	delay_ms(10);
	if ((r = i2c_write_reg(BNO055_ADDR, BNO_OPR_MODE, BNO_MODE_NDOF)))
		return r;
	delay_ms(20);   /* config -> fusion mode switch takes up to 7 ms; 20 is safe */
	return 0;
}

int bno055_info(struct bno055_info *info)
{
	uint8_t b[7];
	int r;
	if ((r = i2c_read_regs(BNO055_ADDR, BNO_CHIP_ID, b, 7)))
		return r;
	info->chip_id = b[0];
	info->acc_id = b[1];
	info->mag_id = b[2];
	info->gyr_id = b[3];
	info->sw_rev = (uint16_t)(b[4] | (b[5] << 8));
	info->bl_rev = b[6];
	if ((r = i2c_read_regs(BNO055_ADDR, BNO_SYS_STATUS, b, 2)))
		return r;
	info->sys_status = b[0];
	info->sys_err = b[1];
	if ((r = i2c_read_regs(BNO055_ADDR, BNO_OPR_MODE, b, 1)))
		return r;
	info->opr_mode = b[0] & 0x0F;
	return 0;
}

int bno055_read_burst(uint8_t *buf)
{
	return i2c_read_regs(BNO055_ADDR, BNO_ACC_DATA, buf, BNO_BURST_LEN);
}

int bno055_calib(uint8_t *calib)
{
	return i2c_read_regs(BNO055_ADDR, BNO_CALIB_STAT, calib, 1);
}
