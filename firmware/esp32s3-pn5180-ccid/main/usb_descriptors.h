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
#define EPNUM_CCID_INT  0x82  // device → host（RDR_to_PC_NotifySlotChange = カード挿抜通知）
#define CCID_EP_SIZE    64    // full-speed bulk
#define CCID_EP_INT_SIZE     8     // NotifySlotChange は 1 + ceil(2*slots/8) byte（13 slot でも 5B）
#define CCID_EP_INT_INTERVAL 0x10  // interrupt polling 間隔（FS: ms 単位, 16ms）

// interrupt-IN（RDR_to_PC_NotifySlotChange）を記述子に載せるか。
//   0 = 載せない（既定）。Windows(usbccid) は GetSlotStatus の polling でカード有無を追う。
//   1 = 載せる。実機（2026-09-10）では Windows が NotifySlotChange(50 03) を読み取るのに
//       present を登録せず（SCardGetStatusChange=EMPTY / connect=0x80100069）、IccPowerOn が
//       一切来なかった。host(RFIDThread) は 100ms polling で非同期通知を使わないため、
//       interrupt は機能的に不要。原因追及のときだけ 1 にする（通知機構のコードは残してある）。
#define CCID_USE_INTERRUPT_EP 0
#define CCID_NUM_ENDPOINTS (2 + CCID_USE_INTERRUPT_EP)

// CCID class descriptor 型番（USB CCID 1.1）。
#define CCID_DESC_TYPE_SMART_CARD 0x21

// esp_tinyusb の tinyusb_config_t に渡す記述子（main.c で使用）。
const tusb_desc_device_t *ccid_device_descriptor(void);
const uint8_t *ccid_configuration_descriptor(void);
const char **ccid_string_descriptors(int *count);
