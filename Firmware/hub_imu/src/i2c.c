/*
 * I2C1 master for the STM32F042 (I2C v2 peripheral), register level.
 * SDA = PF0, SCL = PF1, alternate function 1. Pull-ups are on the board.
 * Clocked from SYSCLK (48 MHz); TIMINGR from RM0091 for 100 kHz standard
 * mode. Clock stretching stays enabled, which the BNO055 needs.
 */
#include "i2c.h"
#include "stm32f0xx.h"

extern volatile uint32_t g_ms;

#define I2C_TIMEOUT_MS 20

static int wait_flag(uint32_t flag)
{
	uint32_t t0 = g_ms;
	for (;;) {
		uint32_t isr = I2C1->ISR;
		if (isr & flag)
			return 0;
		if (isr & I2C_ISR_NACKF) {
			I2C1->ICR = I2C_ICR_NACKCF | I2C_ICR_STOPCF;
			return -1;
		}
		if (g_ms - t0 > I2C_TIMEOUT_MS) {
			/* recover: software reset of the peripheral */
			I2C1->CR1 &= ~I2C_CR1_PE;
			while (I2C1->CR1 & I2C_CR1_PE)
				;
			I2C1->CR1 |= I2C_CR1_PE;
			return -2;
		}
	}
}

void i2c_init(void)
{
	RCC->AHBENR |= RCC_AHBENR_GPIOFEN;
	/* PF0/PF1: alternate function, open drain, high speed, no internal pull */
	GPIOF->MODER = (GPIOF->MODER & ~(GPIO_MODER_MODER0 | GPIO_MODER_MODER1)) |
		       GPIO_MODER_MODER0_1 | GPIO_MODER_MODER1_1;
	GPIOF->OTYPER |= GPIO_OTYPER_OT_0 | GPIO_OTYPER_OT_1;
	GPIOF->OSPEEDR |= GPIO_OSPEEDR_OSPEEDR0 | GPIO_OSPEEDR_OSPEEDR1;
	GPIOF->AFR[0] = (GPIOF->AFR[0] & ~(0xFu | 0xF0u)) | (1u << 0) | (1u << 4);

	RCC->CFGR3 |= RCC_CFGR3_I2C1SW;        /* I2C1 clock = SYSCLK (48 MHz) */
	RCC->APB1ENR |= RCC_APB1ENR_I2C1EN;

	I2C1->CR1 = 0;
	I2C1->TIMINGR = 0xB0420F13u;           /* 100 kHz @ 48 MHz, RM0091 table */
	I2C1->CR1 = I2C_CR1_PE;
}

static void start(uint8_t addr, uint8_t nbytes, uint32_t flags)
{
	I2C1->CR2 = ((uint32_t)addr << 1) | ((uint32_t)nbytes << 16) | I2C_CR2_START | flags;
}

int i2c_write_reg(uint8_t addr, uint8_t reg, uint8_t value)
{
	int r;
	I2C1->ICR = I2C_ICR_STOPCF | I2C_ICR_NACKCF;
	start(addr, 2, I2C_CR2_AUTOEND);
	if ((r = wait_flag(I2C_ISR_TXIS)))
		return r;
	I2C1->TXDR = reg;
	if ((r = wait_flag(I2C_ISR_TXIS)))
		return r;
	I2C1->TXDR = value;
	if ((r = wait_flag(I2C_ISR_STOPF)))
		return r;
	I2C1->ICR = I2C_ICR_STOPCF;
	return 0;
}

int i2c_read_regs(uint8_t addr, uint8_t reg, uint8_t *buf, uint8_t len)
{
	int r;
	I2C1->ICR = I2C_ICR_STOPCF | I2C_ICR_NACKCF;
	/* register address, no stop */
	start(addr, 1, 0);
	if ((r = wait_flag(I2C_ISR_TXIS)))
		return r;
	I2C1->TXDR = reg;
	if ((r = wait_flag(I2C_ISR_TC)))
		return r;
	/* repeated start, read len bytes, auto stop */
	start(addr, len, I2C_CR2_RD_WRN | I2C_CR2_AUTOEND);
	for (uint8_t i = 0; i < len; i++) {
		if ((r = wait_flag(I2C_ISR_RXNE)))
			return r;
		buf[i] = (uint8_t)I2C1->RXDR;
	}
	if ((r = wait_flag(I2C_ISR_STOPF)))
		return r;
	I2C1->ICR = I2C_ICR_STOPCF;
	return 0;
}
