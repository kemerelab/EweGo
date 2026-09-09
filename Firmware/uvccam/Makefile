# ewego-cam: static, no dependencies beyond libc and kernel headers.
#   make                                  native
#   make CC=aarch64-linux-gnu-gcc         cross for 64-bit Pi OS (what CI does)
CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra -Werror
LDFLAGS ?= -static

all: ewego-cam

ewego-cam: ewego-cam.c
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $<

clean:
	rm -f ewego-cam

.PHONY: all clean
