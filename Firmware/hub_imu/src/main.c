/*
 * hub_imu firmware, step 1: enumerate as a composite USB device (CDC console
 * + HID data interface), blink the LED, and answer on the console.
 *
 * Nothing talks to the BNO055 yet. The HID interface is present so the host
 * side can be developed against the final descriptor; it sends nothing.
 *
 * Clocks: HSI48 is both SYSCLK and the USB clock, trimmed by the CRS from
 * USB start-of-frame (the board has no crystal). PA11/PA12 must be remapped
 * to the USB pads (SYSCFG_CFGR1.PA11_PA12_RMP) or the device never appears.
 *
 * Console commands: help, version, dfu (reboot into the USB bootloader).
 */
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include "stm32f0xx.h"
#include "tusb.h"
#include "usb_descriptors.h"

#ifndef FW_VERSION
#define FW_VERSION "dev"
#endif

uint32_t SystemCoreClock = 48000000u;
static volatile uint32_t g_ms;

extern void request_bootloader(void);

/* ---- clocks and pins ----------------------------------------------------- */

static void clock_init(void)
{
	/* HSI48 on, as SYSCLK; flash needs one wait state at 48 MHz */
	RCC->CR2 |= RCC_CR2_HSI48ON;
	while (!(RCC->CR2 & RCC_CR2_HSI48RDY))
		;
	FLASH->ACR = (FLASH->ACR & ~FLASH_ACR_LATENCY) | FLASH_ACR_LATENCY | FLASH_ACR_PRFTBE;
	RCC->CFGR = (RCC->CFGR & ~RCC_CFGR_SW) | RCC_CFGR_SW_HSI48;
	while ((RCC->CFGR & RCC_CFGR_SWS) != RCC_CFGR_SWS_HSI48)
		;
	/* USB clock = HSI48 (USBSW = 0) */
	RCC->CFGR3 &= ~RCC_CFGR3_USBSW;

	/* CRS: trim HSI48 from USB SOF. Reset values of CRS_CFGR already select
	 * USB SOF as the sync source with the right reload for 48 MHz / 1 kHz. */
	RCC->APB1ENR |= RCC_APB1ENR_CRSEN;
	CRS->CR |= CRS_CR_AUTOTRIMEN | CRS_CR_CEN;

	/* peripherals */
	RCC->AHBENR |= RCC_AHBENR_GPIOAEN;
	RCC->APB2ENR |= RCC_APB2ENR_SYSCFGCOMPEN;
	RCC->APB1ENR |= RCC_APB1ENR_USBEN;

	/* USB pads on PA11/PA12 */
	SYSCFG->CFGR1 |= SYSCFG_CFGR1_PA11_PA12_RMP;

	/* LED on PA3, output, push-pull */
	GPIOA->MODER = (GPIOA->MODER & ~GPIO_MODER_MODER3) | GPIO_MODER_MODER3_0;

	/* 1 kHz SysTick */
	SysTick_Config(SystemCoreClock / 1000u);
}

static inline void led(int on)
{
	if (on)
		GPIOA->BSRR = GPIO_BSRR_BS_3;
	else
		GPIOA->BSRR = GPIO_BSRR_BR_3;
}

void SysTick_Handler(void)
{
	g_ms++;
}

void USB_IRQHandler(void)
{
	tud_int_handler(0);
}

/* ---- console ------------------------------------------------------------- */

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

static void banner(void)
{
	con_puts("\r\nhub_imu " FW_VERSION " (STM32F042 + BNO055)\r\n"
		 "step 1: USB only, no sensor yet. type 'help'\r\n> ");
}

static void handle_line(char *line)
{
	if (!strcmp(line, "help")) {
		con_puts("commands: help | version | dfu (reboot into USB bootloader)\r\n");
	} else if (!strcmp(line, "version")) {
		con_puts("hub_imu " FW_VERSION "\r\n");
	} else if (!strcmp(line, "dfu")) {
		con_puts("rebooting into the system bootloader (0483:df11)...\r\n");
		tud_task();
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

/* ---- main ---------------------------------------------------------------- */

int main(void)
{
	clock_init();
	led(1);
	tud_init(0);

	uint32_t last_blink = 0;
	int led_on = 1;
	for (;;) {
		tud_task();
		console_task();

		/* 1 Hz blink once the host has configured us, 5 Hz while waiting */
		uint32_t period = tud_mounted() ? 500 : 100;
		if (g_ms - last_blink >= period) {
			last_blink = g_ms;
			led_on = !led_on;
			led(led_on);
		}
	}
}
