/* USB descriptors: composite CDC (console) + HID (IMU data) */
#include "tusb.h"
#include "usb_descriptors.h"

/* 0x1209 is pid.codes; 0x0001 is its "for testing" PID, fine for a lab tool.
 * Change to an allocated PID before distributing hardware. */
#define USB_VID 0x1209
#define USB_PID 0x0001

enum { ITF_NUM_CDC = 0, ITF_NUM_CDC_DATA, ITF_NUM_HID, ITF_NUM_TOTAL };

#define EPNUM_CDC_NOTIF 0x81
#define EPNUM_CDC_OUT   0x02
#define EPNUM_CDC_IN    0x82
#define EPNUM_HID_IN    0x83

static const tusb_desc_device_t desc_device = {
	.bLength = sizeof(tusb_desc_device_t),
	.bDescriptorType = TUSB_DESC_DEVICE,
	.bcdUSB = 0x0200,
	/* Composite device: class/subclass/protocol per interface (IAD) */
	.bDeviceClass = TUSB_CLASS_MISC,
	.bDeviceSubClass = MISC_SUBCLASS_COMMON,
	.bDeviceProtocol = MISC_PROTOCOL_IAD,
	.bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE,
	.idVendor = USB_VID,
	.idProduct = USB_PID,
	.bcdDevice = 0x0001,
	.iManufacturer = 1,
	.iProduct = 2,
	.iSerialNumber = 3,
	.bNumConfigurations = 1,
};

const uint8_t *tud_descriptor_device_cb(void)
{
	return (const uint8_t *)&desc_device;
}

/* HID: one 64-byte vendor-defined input report (the IMU sample). */
const uint8_t desc_hid_report[] = {
	HID_USAGE_PAGE_N(HID_USAGE_PAGE_VENDOR, 2),
	HID_USAGE(0x01),
	HID_COLLECTION(HID_COLLECTION_APPLICATION),
		HID_USAGE(0x02),
		HID_LOGICAL_MIN(0x00),
		HID_LOGICAL_MAX_N(0x00ff, 2),
		HID_REPORT_SIZE(8),
		HID_REPORT_COUNT(HID_REPORT_BYTES),
		HID_INPUT(HID_DATA | HID_VARIABLE | HID_ABSOLUTE),
	HID_COLLECTION_END,
};

const uint8_t *tud_hid_descriptor_report_cb(uint8_t itf)
{
	(void)itf;
	return desc_hid_report;
}

#define CONFIG_TOTAL_LEN (TUD_CONFIG_DESC_LEN + TUD_CDC_DESC_LEN + TUD_HID_DESC_LEN)

static const uint8_t desc_configuration[] = {
	/* config: 1 configuration, interfaces, string index 0, total length, attributes, power 100 mA */
	TUD_CONFIG_DESCRIPTOR(1, ITF_NUM_TOTAL, 0, CONFIG_TOTAL_LEN, 0x00, 100),
	/* CDC: interface number, string index, notification EP + size, data OUT/IN EPs + size */
	TUD_CDC_DESCRIPTOR(ITF_NUM_CDC, 4, EPNUM_CDC_NOTIF, 8, EPNUM_CDC_OUT, EPNUM_CDC_IN, 64),
	/* HID: interface number, string index, protocol, report descriptor length, EP IN + size, polling interval 1 ms */
	TUD_HID_DESCRIPTOR(ITF_NUM_HID, 5, HID_ITF_PROTOCOL_NONE, sizeof(desc_hid_report), EPNUM_HID_IN, CFG_TUD_HID_EP_BUFSIZE, 1),
};

const uint8_t *tud_descriptor_configuration_cb(uint8_t index)
{
	(void)index;
	return desc_configuration;
}

/* strings */
static const char *const string_desc_arr[] = {
	(const char[]){0x09, 0x04},   /* 0: English (US) */
	"EweGo",                      /* 1: manufacturer */
	"hub_imu BNO055",             /* 2: product */
	"000000000000",               /* 3: serial, replaced by the MCU unique ID */
	"hub_imu console",            /* 4: CDC interface */
	"hub_imu data",               /* 5: HID interface */
};

static uint16_t desc_str[32 + 1];

static void uid_to_hex(char *out)
{
	/* 96-bit unique device ID at 0x1FFFF7AC on STM32F0 */
	const uint32_t *uid = (const uint32_t *)0x1FFFF7ACu;
	static const char hex[] = "0123456789ABCDEF";
	uint32_t w = uid[0] ^ uid[1] ^ uid[2];
	for (int i = 7; i >= 0; i--) {
		out[i] = hex[w & 0xF];
		w >>= 4;
	}
	out[8] = 0;
}

const uint16_t *tud_descriptor_string_cb(uint8_t index, uint16_t langid)
{
	(void)langid;
	uint8_t chr_count;

	if (index == 0) {
		memcpy(&desc_str[1], string_desc_arr[0], 2);
		chr_count = 1;
	} else {
		char serial[9];
		const char *str;
		if (index >= sizeof(string_desc_arr) / sizeof(string_desc_arr[0]))
			return NULL;
		if (index == 3) {
			uid_to_hex(serial);
			str = serial;
		} else {
			str = string_desc_arr[index];
		}
		chr_count = (uint8_t)strlen(str);
		if (chr_count > 32)
			chr_count = 32;
		for (uint8_t i = 0; i < chr_count; i++)
			desc_str[1 + i] = str[i];
	}
	desc_str[0] = (uint16_t)((TUSB_DESC_STRING << 8) | (2 * chr_count + 2));
	return desc_str;
}
