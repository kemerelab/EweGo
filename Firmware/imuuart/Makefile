# ewego-imu-uart: static, libc only. Pulls in the shared recorder skeleton
# ../common/ewego-rec.c (one compile line, no objects left behind).
#   make                                  native
#   make CC=aarch64-linux-gnu-gcc         cross for 64-bit Pi OS (what CI does)
CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -Werror -pthread
LDFLAGS ?= -static -s -pthread
COMMON  := ../common/ewego-rec.c
COMMON_H := ../common/ewego-rec.h

all: ewego-imu-uart

ewego-imu-uart: ewego-imu-uart.c $(COMMON) $(COMMON_H)
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ ewego-imu-uart.c $(COMMON)

clean:
	rm -f ewego-imu-uart

.PHONY: all clean
