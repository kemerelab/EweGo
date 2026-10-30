#ifndef HUBIMU_I2C_H
#define HUBIMU_I2C_H
#include <stdint.h>

/* I2C1 master on PF0 (SDA) / PF1 (SCL), 400 kHz, blocking with timeouts.
 * Return 0 on success, negative on NACK or timeout. */
void i2c_init(void);
int i2c_write_reg(uint8_t addr, uint8_t reg, uint8_t value);
int i2c_read_regs(uint8_t addr, uint8_t reg, uint8_t *buf, uint8_t len);

#endif
