# hub_imu firmware — STM32F042F6P6 (Cortex-M0, 32 KB flash, 6 KB RAM)
# Bare CMSIS + TinyUSB. Dependencies come from ./fetch-deps.sh into lib/.
#
#   ./fetch-deps.sh && make        -> build/hub_imu.bin (+ .elf, .map, size report)
#   dfu-util -a 0 -s 0x08000000:leave -D build/hub_imu.bin

TARGET  := hub_imu
BUILD   := build
PREFIX  ?= arm-none-eabi-
CC      := $(PREFIX)gcc
OBJCOPY := $(PREFIX)objcopy
SIZE    := $(PREFIX)size

TUSB    := lib/tinyusb/src
CMSIS_DEV := lib/cmsis_device_f0/Include
CMSIS_CORE := lib/cmsis_core/Include

SRCS := \
	src/startup.c \
	src/main.c \
	src/usb_descriptors.c \
	src/i2c.c \
	src/bno055.c \
	$(TUSB)/tusb.c \
	$(TUSB)/common/tusb_fifo.c \
	$(TUSB)/device/usbd.c \
	$(TUSB)/class/cdc/cdc_device.c \
	$(TUSB)/class/hid/hid_device.c \
	$(TUSB)/portable/st/stm32_fsdev/fsdev_common.c \
	$(TUSB)/portable/st/stm32_fsdev/dcd_stm32_fsdev.c

OBJS := $(patsubst %.c,$(BUILD)/%.o,$(SRCS))

GIT_VERSION := $(shell git describe --tags --always --dirty 2>/dev/null || echo unknown)

CFLAGS := -mcpu=cortex-m0 -mthumb -Os -g3 -std=gnu11 \
	-ffunction-sections -fdata-sections -fno-common -fno-strict-aliasing \
	-Wall -Wextra -Wno-unused-parameter \
	-DSTM32F042x6 -DCFG_TUSB_MCU=OPT_MCU_STM32F0 -DCFG_TUSB_OS=OPT_OS_NONE \
	-DFW_VERSION=\"$(GIT_VERSION)\" \
	-Isrc -I$(TUSB) -I$(CMSIS_DEV) -I$(CMSIS_CORE)

LDFLAGS := -mcpu=cortex-m0 -mthumb -nostartfiles -specs=nano.specs -specs=nosys.specs \
	-Wl,--gc-sections -Wl,-Map=$(BUILD)/$(TARGET).map -T src/STM32F042X6.ld

all: $(BUILD)/$(TARGET).bin size

$(BUILD)/%.o: %.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -MMD -MP -c $< -o $@

$(BUILD)/$(TARGET).elf: $(OBJS)
	$(CC) $(LDFLAGS) $^ -o $@

$(BUILD)/$(TARGET).bin: $(BUILD)/$(TARGET).elf
	$(OBJCOPY) -O binary $< $@

size: $(BUILD)/$(TARGET).elf
	$(SIZE) $<

clean:
	rm -rf $(BUILD)

-include $(OBJS:.o=.d)
.PHONY: all size clean
