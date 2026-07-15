#!/usr/bin/env python3
"""
Diagnose whether cbreak-mode single-key input works in this terminal.

Run it, press keys, press 'q' to quit. Every keypress prints:
   raw bytes -> decoded char -> elapsed since previous key

If keys only register after pressing Enter → line-buffering is still on
(cbreak didn't apply). If keys never register → stdin isn't a TTY, or
select/os.read is being intercepted (multiplexer? IME? sudo?).
"""
import os, select, sys, termios, time, tty

print(f"isatty={sys.stdin.isatty()}  fd={sys.stdin.fileno()}  "
      f"TERM={os.environ.get('TERM')!r}  "
      f"pgid={os.getpgrp()}  fgpgid={os.tcgetpgrp(0) if sys.stdin.isatty() else 'n/a'}")

if not sys.stdin.isatty():
    print("stdin is not a TTY — cbreak cannot apply. Are you piping input?")
    sys.exit(1)

fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
tty.setcbreak(fd)
new = termios.tcgetattr(fd)
print(f"lflag before=0x{old[3]:x}  after=0x{new[3]:x}  "
      f"VMIN={new[6][termios.VMIN]}  VTIME={new[6][termios.VTIME]}")
print("press keys ('q' to quit)...")

try:
    last = time.monotonic()
    while True:
        r, _, _ = select.select([fd], [], [], 5.0)
        now = time.monotonic()
        if not r:
            print(f"  [no key in 5.0s]  (elapsed={now-last:.2f}s)")
            last = now
            continue
        data = os.read(fd, 8)  # read up to 8 bytes to catch escape sequences
        dt = now - last
        last = now
        print(f"  bytes={data!r}  decoded={data.decode('utf-8','replace')!r}  "
              f"dt={dt*1000:.0f}ms")
        if data == b'q':
            break
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
    print("restored.")
