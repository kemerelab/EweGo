/*
 * hub_imu firmware, step 3: streams one BNO055 sample per HID report at
 * 100 Hz (see report.h), with a device microsecond timestamp and sequence
 * counter, and keeps the CDC console for inspection.
 *
 * Clocks: HSI48 is both SYSCLK and the USB clock, trimmed by the CRS from
 * USB start-of-frame (the board has no crystal). PA11/PA12 must be remapped
 * to the USB pads (SYSCFG_CFGR1.PA11_PA12_RMP) or the device never appears.
 *
 * Console commands: help, version, id, cal, read, init, stats, stream, dfu.
 */
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include "stm32f0xx.h"
#include "tusb.h"
#include "usb_descriptors.h"
#include "i2c.h"
#include "bno055.h"
#include "report.h"

#ifndef FW_VERSION
#define FW_VERSION "dev"
#endif

uint32_t SystemCoreClock = 48000000u;
volatile uint32_t g_ms;
static int g_sensor_ok;      /* 0 = not initialised / failed, 1 = NDOF running */
static int g_sensor_err;     /* last bno055_init() result */

/* sampling */
#define SAMPLE_PERIOD_US 10000u   /* 100 Hz */
static int g_stream = 1;          /* HID streaming enabled */
static uint16_t g_seq;
static uint32_t g_next_sample_us;
static uint32_t g_sent, g_i2c_err, g_hid_busy, g_i2c_us_max;
static int g_last_dropped;

extern void request_bootloader(void);

/* ---- clocks and pins ----------------------------------------------------- */

static void clock_init(void)
{
	RCC->CR2 |= RCC_CR2_HSI48ON;
	while (!(RCC->CR2 & RCC_CR2_HSI48RDY))
		;
	FLASH->ACR = (FLASH->ACR & ~FLASH_ACR_LATENCY) | FLASH_ACR_LATENCY | FLASH_ACR_PRFTBE;
	RCC->CFGR = (RCC->CFGR & ~RCC_CFGR_SW) | RCC_CFGR_SW_HSI48;
	while ((RCC->CFGR & RCC_CFGR_SWS) != RCC_CFGR_SWS_HSI48)
		;
	RCC->CFGR3 &= ~RCC_CFGR3_USBSW;          /* USB clock = HSI48 */

	RCC->APB1ENR |= RCC_APB1ENR_CRSEN;       /* trim HSI48 from USB SOF */
	CRS->CR |= CRS_CR_AUTOTRIMEN | CRS_CR_CEN;

	RCC->AHBENR |= RCC_AHBENR_GPIOAEN;
	RCC->APB2ENR |= RCC_APB2ENR_SYSCFGCOMPEN;
	RCC->APB1ENR |= RCC_APB1ENR_USBEN;
	SYSCFG->CFGR1 |= SYSCFG_CFGR1_PA11_PA12_RMP;

	GPIOA->MODER = (GPIOA->MODER & ~GPIO_MODER_MODER3) | GPIO_MODER_MODER3_0;   /* LED */

	/* TIM2: 32-bit free-running microsecond clock (device timestamp) */
	RCC->APB1ENR |= RCC_APB1ENR_TIM2EN;
	TIM2->PSC = (SystemCoreClock / 1000000u) - 1;
	TIM2->ARR = 0xFFFFFFFFu;
	TIM2->EGR = TIM_EGR_UG;
	TIM2->CR1 = TIM_CR1_CEN;

	SysTick_Config(SystemCoreClock / 1000u);
}

static inline uint32_t now_us(void)
{
	return TIM2->CNT;
}

static inline void led(int on)
{
	GPIOA->BSRR = on ? GPIO_BSRR_BS_3 : GPIO_BSRR_BR_3;
}

void SysTick_Handler(void)
{
	g_ms++;
}

void USB_IRQHandler(void)
{
	tud_int_handler(0);
}

void bno055_yield(void)
{
	tud_task();
}

/* ---- console output ------------------------------------------------------- */

static void con_puts(const char *s)
{
	if (!tud_cdc_connected())
		return;
	while (*s) {
		uint32_t n = tud_cdc_write_available();
		if (n == 0) {
			tud_cdc_write_flush();
			tud_task();
			continue;
		}
		uint32_t len = strlen(s);
		if (len > n)
			len = n;
		tud_cdc_write(s, len);
		s += len;
	}
	tud_cdc_write_flush();
}

static char g_line_buf[96];
#define con_printf(...) do { snprintf(g_line_buf, sizeof(g_line_buf), __VA_ARGS__); con_puts(g_line_buf); } while (0)

/* print a signed 16-bit raw value scaled by 1/div as a decimal (no float printf) */
static void con_fixed(const char *label, int16_t raw, int div, int decimals)
{
	int32_t v = raw;
	int neg = v < 0;
	if (neg)
		v = -v;
	int32_t ip = v / div, fp = v % div;
	/* scale the fraction to the requested number of decimals */
	int32_t scale = 1;
	for (int i = 0; i < decimals; i++)
		scale *= 10;
	fp = (fp * scale + div / 2) / div;
	if (fp >= scale) {
		ip++;
		fp -= scale;
	}
	if (decimals)
		con_printf("%s%s%ld.%0*ld", label, neg ? "-" : "", (long)ip, decimals, (long)fp);
	else
		con_printf("%s%s%ld", label, neg ? "-" : "", (long)ip);
}

static const char *sensor_state(void)
{
	if (g_sensor_ok)
		return "NDOF running";
	switch (g_sensor_err) {
	case 1: return "wrong chip ID";
	case -1: return "no response on I2C (check 3V3, SDA/SCL, address 0x28)";
	case -2: return "I2C timeout";
	default: return "init failed";
	}
}

static void banner(void)
{
	con_puts("\r\nhub_imu " FW_VERSION " (STM32F042 + BNO055)\r\n");
	con_printf("sensor: %s. type 'help'\r\n> ", sensor_state());
}

/* ---- commands ------------------------------------------------------------- */

static void cmd_id(void)
{
	struct bno055_info info;
	int r = bno055_info(&info);
	if (r) {
		con_printf("I2C error %d (%s)\r\n", r, sensor_state());
		return;
	}
	con_printf("chip 0x%02X (expect 0xA0)  acc 0x%02X  mag 0x%02X  gyr 0x%02X\r\n",
		   info.chip_id, info.acc_id, info.mag_id, info.gyr_id);
	con_printf("sw rev 0x%04X  bootloader 0x%02X  opr_mode 0x%02X  sys_status %u  sys_err %u\r\n",
		   info.sw_rev, info.bl_rev, info.opr_mode, info.sys_status, info.sys_err);
	con_puts("sys_status: 5 = fusion running, 1 = system error (see sys_err)\r\n");
}

static void cmd_cal(void)
{
	uint8_t c;
	int r = bno055_calib(&c);
	if (r) {
		con_printf("I2C error %d\r\n", r);
		return;
	}
	con_printf("calibration (0..3): sys %u  gyr %u  acc %u  mag %u\r\n",
		   (c >> 6) & 3, (c >> 4) & 3, (c >> 2) & 3, c & 3);
}

static int16_t le16(const uint8_t *p)
{
	return (int16_t)(p[0] | (p[1] << 8));
}

static void cmd_read(void)
{
	uint8_t b[BNO_BURST_LEN];
	uint32_t t0 = g_ms;
	int r = bno055_read_burst(b);
	uint32_t dt = g_ms - t0;
	if (r) {
		con_printf("I2C error %d\r\n", r);
		return;
	}
	/* offsets relative to 0x08 */
	con_puts("acc m/s^2  ");
	con_fixed("", le16(b + 0), 100, 2); con_fixed(" ", le16(b + 2), 100, 2); con_fixed(" ", le16(b + 4), 100, 2);
	con_puts("\r\nmag uT     ");
	con_fixed("", le16(b + 6), 16, 1); con_fixed(" ", le16(b + 8), 16, 1); con_fixed(" ", le16(b + 10), 16, 1);
	con_puts("\r\ngyr dps    ");
	con_fixed("", le16(b + 12), 16, 1); con_fixed(" ", le16(b + 14), 16, 1); con_fixed(" ", le16(b + 16), 16, 1);
	con_puts("\r\neuler deg  h/r/p ");
	con_fixed("", le16(b + 18), 16, 1); con_fixed(" ", le16(b + 20), 16, 1); con_fixed(" ", le16(b + 22), 16, 1);
	con_puts("\r\nquat w/x/y/z ");
	con_fixed("", le16(b + 24), 16384, 4); con_fixed(" ", le16(b + 26), 16384, 4);
	con_fixed(" ", le16(b + 28), 16384, 4); con_fixed(" ", le16(b + 30), 16384, 4);
	con_puts("\r\nlin acc    ");
	con_fixed("", le16(b + 32), 100, 2); con_fixed(" ", le16(b + 34), 100, 2); con_fixed(" ", le16(b + 36), 100, 2);
	con_puts("\r\ngravity    ");
	con_fixed("", le16(b + 38), 100, 2); con_fixed(" ", le16(b + 40), 100, 2); con_fixed(" ", le16(b + 42), 100, 2);
	con_printf("\r\ntemp %d C   calib sys %u gyr %u acc %u mag %u   (46-byte burst took %lu ms)\r\n",
		   (int8_t)b[44], (b[45] >> 6) & 3, (b[45] >> 4) & 3, (b[45] >> 2) & 3, b[45] & 3, (unsigned long)dt);
}

static void cmd_init(void)
{
	g_sensor_err = bno055_init();
	g_sensor_ok = g_sensor_err == 0;
	con_printf("init: %s\r\n", sensor_state());
}

static void cmd_stats(void)
{
	con_printf("stream %s, %u Hz: sent %lu, i2c errors %lu, hid busy (host not polling) %lu, "
		   "i2c burst max %lu us, seq %u, uptime %lu s\r\n",
		   g_stream ? "on" : "off", 1000000u / SAMPLE_PERIOD_US, (unsigned long)g_sent,
		   (unsigned long)g_i2c_err, (unsigned long)g_hid_busy, (unsigned long)g_i2c_us_max,
		   g_seq, (unsigned long)(g_ms / 1000u));
	g_i2c_us_max = 0;
}

static void handle_line(char *line)
{
	if (!strcmp(line, "help")) {
		con_puts("help | version | id | cal | read | init | stats | stream (toggle HID) | dfu\r\n");
	} else if (!strcmp(line, "version")) {
		con_printf("hub_imu " FW_VERSION ", sensor: %s\r\n", sensor_state());
	} else if (!strcmp(line, "id")) {
		cmd_id();
	} else if (!strcmp(line, "cal")) {
		cmd_cal();
	} else if (!strcmp(line, "read")) {
		cmd_read();
	} else if (!strcmp(line, "init")) {
		cmd_init();
	} else if (!strcmp(line, "stats")) {
		cmd_stats();
	} else if (!strcmp(line, "stream")) {
		g_stream = !g_stream;
		con_printf("HID stream %s\r\n", g_stream ? "on" : "off");
	} else if (!strcmp(line, "dfu")) {
		con_puts("rebooting into the system bootloader (0483:df11)...\r\n");
		for (uint32_t t = g_ms; g_ms - t < 50;)
			tud_task();
		request_bootloader();
	} else if (line[0]) {
		con_puts("unknown command; try 'help'\r\n");
	}
	con_puts("> ");
}

static void console_task(void)
{
	static char line[32];
	static uint8_t len;

	while (tud_cdc_available()) {
		char c;
		if (tud_cdc_read(&c, 1) != 1)
			break;
		if (c == '\r' || c == '\n') {
			con_puts("\r\n");
			line[len] = 0;
			handle_line(line);
			len = 0;
		} else if (c == 8 || c == 127) {
			if (len) {
				len--;
				con_puts("\b \b");
			}
		} else if (len < sizeof(line) - 1 && c >= 32) {
			line[len++] = c;
			tud_cdc_write_char(c);
			tud_cdc_write_flush();
		}
	}
}

void tud_cdc_line_state_cb(uint8_t itf, bool dtr, bool rts)
{
	(void)itf;
	(void)rts;
	if (dtr)
		banner();
}

/* ---- HID callbacks (required by TinyUSB; nothing to report yet) ---------- */

uint16_t tud_hid_get_report_cb(uint8_t itf, uint8_t report_id, hid_report_type_t report_type,
			       uint8_t *buffer, uint16_t reqlen)
{
	(void)itf; (void)report_id; (void)report_type; (void)buffer; (void)reqlen;
	return 0;
}

void tud_hid_set_report_cb(uint8_t itf, uint8_t report_id, hid_report_type_t report_type,
			   const uint8_t *buffer, uint16_t bufsize)
{
	(void)itf; (void)report_id; (void)report_type; (void)buffer; (void)bufsize;
}

/* ---- sampling ------------------------------------------------------------- */

static void sample_task(void)
{
	static struct imu_report rpt;
	uint32_t now = now_us();

	if (!g_stream || (int32_t)(now - g_next_sample_us) < 0)
		return;
	g_next_sample_us += SAMPLE_PERIOD_US;
	/* if we fell far behind (console output, USB stall), resynchronise rather
	 * than burst-catch-up */
	if ((int32_t)(now - g_next_sample_us) > (int32_t)SAMPLE_PERIOD_US)
		g_next_sample_us = now + SAMPLE_PERIOD_US;

	memset(&rpt, 0, sizeof(rpt));
	rpt.version = REPORT_VERSION;
	rpt.seq = g_seq++;
	rpt.period_us = SAMPLE_PERIOD_US;
	if (g_last_dropped)
		rpt.flags |= RPT_FLAG_DROPPED;

	if (!g_sensor_ok) {
		rpt.flags |= RPT_FLAG_NO_SENSOR;
		rpt.dev_ts_us = now_us();
	} else {
		uint32_t t0 = now_us();
		rpt.dev_ts_us = t0;
		if (bno055_read_burst(rpt.burst)) {
			rpt.flags |= RPT_FLAG_I2C_ERROR;
			g_i2c_err++;
		}
		uint32_t dt = now_us() - t0;
		rpt.i2c_us = dt > 0xFFFF ? 0xFFFF : (uint16_t)dt;
		if (dt > g_i2c_us_max)
			g_i2c_us_max = dt;
	}

	if (tud_hid_ready()) {
		tud_hid_report(0, &rpt, sizeof(rpt));
		g_sent++;
		g_last_dropped = 0;
	} else {
		/* previous report still in the endpoint: host is not polling */
		g_hid_busy++;
		g_last_dropped = 1;
	}
}

/* ---- main ---------------------------------------------------------------- */

int main(void)
{
	clock_init();
	led(1);
	const tusb_rhport_init_t rh_init = {.role = TUSB_ROLE_DEVICE, .speed = TUSB_SPEED_FULL};
	tusb_rhport_init(0, &rh_init);

	/* The sensor takes ~700 ms to boot; bno055_init() calls bno055_yield()
	 * while it waits, so USB enumeration proceeds in the meantime. */
	i2c_init();
	g_sensor_err = bno055_init();
	g_sensor_ok = g_sensor_err == 0;

	g_next_sample_us = now_us() + SAMPLE_PERIOD_US;
	uint32_t last_blink = 0;
	int led_on = 1;
	for (;;) {
		tud_task();
		console_task();
		sample_task();

		/* 1 Hz once configured by the host (2 Hz if the sensor failed),
		 * 5 Hz while waiting for the host */
		uint32_t period = tud_mounted() ? (g_sensor_ok ? 500 : 250) : 100;
		if (g_ms - last_blink >= period) {
			last_blink = g_ms;
			led_on = !led_on;
			led(led_on);
		}
	}
}
