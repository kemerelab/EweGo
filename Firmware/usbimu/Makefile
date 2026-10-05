# ewego-imu-usb: static, libc only.
#   make                                  native
#   make CC=aarch64-linux-gnu-gcc         cross for 64-bit Pi OS (what CI does)
CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -Werror
LDFLAGS ?= -static -s

all: ewego-imu-usb

ewego-imu-usb: ewego-imu-usb.c
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $<

clean:
	rm -f ewego-imu-usb

.PHONY: all clean
