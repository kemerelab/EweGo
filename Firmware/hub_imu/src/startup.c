/*
 * Minimal startup for STM32F042: vector table, reset handler, and the
 * "reboot into the system bootloader" path.
 *
 * The bootloader request works like this: the application stores a magic
 * word in a RAM location that is not touched by startup (.noinit) and
 * performs a system reset. Reset_Handler sees the magic before anything
 * else runs, clears it, maps system memory to address 0 and jumps to the
 * bootloader, which then enumerates as USB DFU (0483:df11) exactly as if
 * BOOT0 had been held high. After that first flash, the jumper is never
 * needed again.
 */
#include <stdint.h>
#include "stm32f0xx.h"

extern uint32_t _estack, _sidata, _sdata, _edata, _sbss, _ebss;
extern int main(void);

#define BOOTLOADER_MAGIC 0xB007F042u
#define SYSTEM_MEMORY_F04X 0x1FFFC400u   /* AN2606: STM32F04xxx bootloader */

uint32_t g_boot_request __attribute__((section(".noinit")));

void request_bootloader(void)
{
	g_boot_request = BOOTLOADER_MAGIC;
	NVIC_SystemReset();
}

static void jump_to_bootloader(void)
{
	const uint32_t *vt = (const uint32_t *)SYSTEM_MEMORY_F04X;
	RCC->APB2ENR |= RCC_APB2ENR_SYSCFGCOMPEN;
	/* MEM_MODE = 01: system flash memory mapped at 0x00000000 */
	SYSCFG->CFGR1 = (SYSCFG->CFGR1 & ~SYSCFG_CFGR1_MEM_MODE) | SYSCFG_CFGR1_MEM_MODE_0;
	__set_MSP(vt[0]);
	((void (*)(void))vt[1])();
	for (;;)
		;
}

void Reset_Handler(void)
{
	if (g_boot_request == BOOTLOADER_MAGIC) {
		g_boot_request = 0;
		jump_to_bootloader();
	}
	g_boot_request = 0;

	uint32_t *src = &_sidata, *dst = &_sdata;
	while (dst < &_edata)
		*dst++ = *src++;
	for (dst = &_sbss; dst < &_ebss;)
		*dst++ = 0;

	main();
	for (;;)
		;
}

void Default_Handler(void)
{
	for (;;)
		;
}

void NMI_Handler(void) __attribute__((weak, alias("Default_Handler")));
void HardFault_Handler(void) __attribute__((weak, alias("Default_Handler")));
void SVC_Handler(void) __attribute__((weak, alias("Default_Handler")));
void PendSV_Handler(void) __attribute__((weak, alias("Default_Handler")));
void SysTick_Handler(void) __attribute__((weak, alias("Default_Handler")));
void USB_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void EXTI4_15_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));
void TIM2_IRQHandler(void) __attribute__((weak, alias("Default_Handler")));

/* Cortex-M0 core vectors + the 32 STM32F042 peripheral IRQs */
__attribute__((section(".isr_vector"), used))
const void *const g_vector_table[16 + 32] = {
	&_estack,
	Reset_Handler,
	NMI_Handler,
	HardFault_Handler,
	0, 0, 0, 0, 0, 0, 0,
	SVC_Handler,
	0, 0,
	PendSV_Handler,
	SysTick_Handler,
	/* IRQ 0.. */
	Default_Handler,        /* 0  WWDG */
	Default_Handler,        /* 1  PVD_VDDIO2 */
	Default_Handler,        /* 2  RTC */
	Default_Handler,        /* 3  FLASH */
	Default_Handler,        /* 4  RCC_CRS */
	Default_Handler,        /* 5  EXTI0_1 */
	Default_Handler,        /* 6  EXTI2_3 */
	EXTI4_15_IRQHandler,    /* 7  EXTI4_15 */
	Default_Handler,        /* 8  TSC */
	Default_Handler,        /* 9  DMA1_Channel1 */
	Default_Handler,        /* 10 DMA1_Channel2_3 */
	Default_Handler,        /* 11 DMA1_Channel4_5 */
	Default_Handler,        /* 12 ADC1 */
	Default_Handler,        /* 13 TIM1_BRK_UP_TRG_COM */
	Default_Handler,        /* 14 TIM1_CC */
	TIM2_IRQHandler,        /* 15 TIM2 */
	Default_Handler,        /* 16 TIM3 */
	Default_Handler,        /* 17 */
	Default_Handler,        /* 18 */
	Default_Handler,        /* 19 TIM14 */
	Default_Handler,        /* 20 */
	Default_Handler,        /* 21 TIM16 */
	Default_Handler,        /* 22 TIM17 */
	Default_Handler,        /* 23 I2C1 */
	Default_Handler,        /* 24 */
	Default_Handler,        /* 25 SPI1 */
	Default_Handler,        /* 26 SPI2 */
	Default_Handler,        /* 27 USART1 */
	Default_Handler,        /* 28 USART2 */
	Default_Handler,        /* 29 */
	Default_Handler,        /* 30 CEC_CAN */
	USB_IRQHandler,         /* 31 USB */
};
