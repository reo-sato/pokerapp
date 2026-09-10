// usb_descriptors.c — CCID smart card reader の USB 記述子（契約 §2）。
//
// TinyUSB は CCID クラスの記述子マクロを持たないため手書きする。値は USB CCID 1.1 仕様 +
// 一般的な非接触リーダー(ACR122U 等)に倣う。host は ATR/記述子の中身を解釈せず reader_name /
// VID-PID を等値照合するだけ（契約 §2/§5）なので、Windows が CCID として bind して
// SCardConnect が通ることが要件。**実機で usbview / probe_pcsc list により要検証**。
#include "tusb.h"
#include "app_config.h"
#include "usb_descriptors.h"

// ───────── Device Descriptor ─────────
static const tusb_desc_device_t s_device_desc = {
    .bLength            = sizeof(tusb_desc_device_t),
    .bDescriptorType    = TUSB_DESC_DEVICE,
    .bcdUSB             = 0x0200,
    // クラスは interface 側(CCID=0x0B)で定義する。
    .bDeviceClass       = 0x00,
    .bDeviceSubClass    = 0x00,
    .bDeviceProtocol    = 0x00,
    .bMaxPacketSize0    = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor           = USB_VID,
    .idProduct          = USB_PID,
    // Windows は VID/PID/REV で記述子をキャッシュする。interrupt EP 追加（0x0101）のように
    // 記述子構成を変えたら REV を上げて再読込させる（reader_name には影響しない, 契約 §2/§3）。
    .bcdDevice          = 0x0101,
    .iManufacturer      = 0x01,
    .iProduct           = 0x02,
    .iSerialNumber      = 0x03,
    .bNumConfigurations = 0x01,
};

const tusb_desc_device_t *ccid_device_descriptor(void) {
    return &s_device_desc;
}

// ───────── CCID functional descriptor（54 byte, USB CCID 1.1 §5.1）─────────
// dwFeatures は Short-APDU level exchange を含む値（ACR122U 由来 0x000204BA）。
// host は XfrBlock で pseudo-APDU `FF CA 00 00 00` を送り、データ+SW を期待する（§6）。
#define CCID_FUNC_DESC                                                       \
    0x36,        /* bLength = 54 */                                          \
    0x21,        /* bDescriptorType = CCID */                                \
    0x10, 0x01,  /* bcdCCID = 1.10 */                                        \
    (CCID_SLOT_COUNT - 1), /* bMaxSlotIndex */                              \
    0x07,        /* bVoltageSupport = 5.0/3.0/1.8V */                        \
    0x03, 0x00, 0x00, 0x00, /* dwProtocols = T=0,T=1 (host は非接触で無視) */ \
    0xFC, 0x0D, 0x00, 0x00, /* dwDefaultClock = 3580 kHz */                  \
    0xFC, 0x0D, 0x00, 0x00, /* dwMaximumClock */                            \
    0x00,        /* bNumClockSupported = 0(=default のみ) */                 \
    0x80, 0x25, 0x00, 0x00, /* dwDataRate = 9600 */                          \
    0x80, 0x25, 0x00, 0x00, /* dwMaxDataRate */                             \
    0x00,        /* bNumDataRatesSupported */                                \
    0xFE, 0x00, 0x00, 0x00, /* dwMaxIFSD = 254 */                            \
    0x00, 0x00, 0x00, 0x00, /* dwSynchProtocols */                          \
    0x00, 0x00, 0x00, 0x00, /* dwMechanical = なし */                        \
    0xBA, 0x04, 0x02, 0x00, /* dwFeatures = 0x000204BA (Short APDU + auto) */ \
    0x0F, 0x01, 0x00, 0x00, /* dwMaxCCIDMessageLength = 271 */               \
    0xFF,        /* bClassGetResponse = echo */                              \
    0xFF,        /* bClassEnvelope = echo */                                 \
    0x00, 0x00,  /* wLcdLayout = なし */                                     \
    0x00,        /* bPINSupport = なし */                                     \
    CCID_SLOT_COUNT /* bMaxCCIDBusySlots */

// ───────── Configuration Descriptor ─────────
// config(9) + interface(9) + CCID func(54) + EP out(7) + EP in(7) + EP int(7) = 93
// interrupt-IN は CCID 仕様上 optional だが、無いと Windows(usbccid) がカード挿入を知る
// 手段が polling 頼みになり、実機では IccPowerOn が一切来なかった（probe_pcsc watch 0 件）。
#define CCID_CONFIG_TOTAL_LEN (9 + 9 + 54 + 7 + 7 + 7)

static const uint8_t s_config_desc[] = {
    // Configuration descriptor
    0x09, TUSB_DESC_CONFIGURATION,
    U16_TO_U8S_LE(CCID_CONFIG_TOTAL_LEN),
    ITF_COUNT,        // bNumInterfaces
    0x01,             // bConfigurationValue
    0x00,             // iConfiguration
    0x80,             // bmAttributes = bus powered
    0x32,             // bMaxPower = 100 mA（実機の消費に合わせる。PN5180 は要確認）

    // Interface descriptor（CCID, class 0x0B）
    0x09, TUSB_DESC_INTERFACE,
    ITF_NUM_CCID,     // bInterfaceNumber
    0x00,             // bAlternateSetting
    0x03,             // bNumEndpoints = bulk OUT + bulk IN + interrupt IN
    TUSB_CLASS_SMART_CARD, // bInterfaceClass = 0x0B（CCID）
    0x00,             // bInterfaceSubClass
    0x00,             // bInterfaceProtocol
    0x00,             // iInterface

    // CCID class descriptor
    CCID_FUNC_DESC,

    // Endpoint: bulk OUT
    0x07, TUSB_DESC_ENDPOINT, EPNUM_CCID_OUT, TUSB_XFER_BULK,
    U16_TO_U8S_LE(CCID_EP_SIZE), 0x00,
    // Endpoint: bulk IN
    0x07, TUSB_DESC_ENDPOINT, EPNUM_CCID_IN, TUSB_XFER_BULK,
    U16_TO_U8S_LE(CCID_EP_SIZE), 0x00,
    // Endpoint: interrupt IN（RDR_to_PC_NotifySlotChange: カード挿抜通知）
    0x07, TUSB_DESC_ENDPOINT, EPNUM_CCID_INT, TUSB_XFER_INTERRUPT,
    U16_TO_U8S_LE(CCID_EP_INT_SIZE), CCID_EP_INT_INTERVAL,
};

const uint8_t *ccid_configuration_descriptor(void) {
    return s_config_desc;
}

// String descriptors（reader_name に product 文字列が現れる, §2/§3）。
// index0 = langid(en-US)。esp_tinyusb が UTF-16 化と tud_descriptor_string_cb を担う。
static const char *s_strings[] = {
    (const char[]){0x09, 0x04},  // 0: langid en-US
    USB_MANUFACTURER_STR,         // 1
    USB_PRODUCT_STR,              // 2: ← OS の reader_name に出る（§2/§3）
    USB_SERIAL_STR,               // 3
};

const char **ccid_string_descriptors(int *count) {
    *count = (int)(sizeof(s_strings) / sizeof(s_strings[0]));
    return s_strings;
}
