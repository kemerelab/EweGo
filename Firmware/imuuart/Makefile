# ewego-imu-uart: static, libc only.
#   make                                  native
#   make CC=aarch64-linux-gnu-gcc         cross for 64-bit Pi OS (what CI does)
CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -Werror -pthread
LDFLAGS ?= -static -s -pthread

all: ewego-imu-uart

ewego-imu-uart: ewego-imu-uart.c
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $<

clean:
	rm -f ewego-imu-uart

.PHONY: all clean
