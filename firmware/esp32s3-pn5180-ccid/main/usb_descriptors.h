// usb_descriptors.h — CCID デバイスの USB 記述子（契約 §2）。
// 記述子の提供は esp_tinyusb(tinyusb_driver_install)に渡す方式。tud_descriptor_*_cb は
// esp_tinyusb 側が config から生成するため、ここでは定義しない（重複シンボル回避）。
#pragma once

#include <stdint.h>
#include "tusb.h"

// CCID インターフェース番号とエンドポイント（bulk IN/OUT）。
#define ITF_NUM_CCID    0
#define ITF_COUNT       1

#define EPNUM_CCID_OUT  0x01  // host → device（CCID コマンド）
#define EPNUM_CCID_IN   0x81  // device → host（CCID レスポンス）
#define CCID_EP_SIZE    64    // full-speed bulk

// CCID class descriptor 型番（USB CCID 1.1）。
#define CCID_DESC_TYPE_SMART_CARD 0x21

// esp_tinyusb の tinyusb_config_t に渡す記述子（main.c で使用）。
const tusb_desc_device_t *ccid_device_descriptor(void);
const uint8_t *ccid_configuration_descriptor(void);
const char **ccid_string_descriptors(int *count);
