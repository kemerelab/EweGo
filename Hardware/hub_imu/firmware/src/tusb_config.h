#ifndef TUSB_CONFIG_H
#define TUSB_CONFIG_H

/* MCU and OS are passed on the compiler command line (Makefile). */

#define CFG_TUSB_MEM_SECTION
#define CFG_TUSB_MEM_ALIGN        __attribute__((aligned(4)))

#define CFG_TUD_ENABLED           1
#define CFG_TUD_MAX_SPEED         OPT_MODE_FULL_SPEED
#define CFG_TUD_ENDPOINT0_SIZE    64

/* Composite device: CDC console (for a laptop or the collar) + HID data. */
#define CFG_TUD_CDC               1
#define CFG_TUD_HID               1
#define CFG_TUD_MSC               0
#define CFG_TUD_MIDI              0
#define CFG_TUD_VENDOR            0

#define CFG_TUD_CDC_RX_BUFSIZE    64
#define CFG_TUD_CDC_TX_BUFSIZE    256
#define CFG_TUD_CDC_EP_BUFSIZE    64

#define CFG_TUD_HID_EP_BUFSIZE    64

#endif
