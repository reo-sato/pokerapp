// app_config.h — 実機に合わせて編集する設定（ピン / slot 数 / USB ID）。
//
// 契約: docs/contracts/rfid-usb-ccid.md v1.0 / docs/rfid-ccid-firmware-checklist.md
// host 側は reader_name / VID-PID を等値照合するだけなので、ここを確定して契約 §2/§4 へ転記する。
#pragma once

#include <stdint.h>
#include "driver/spi_master.h"

// ───────── CCID slot 数（= PN5180 の台数）─────────
// MVP は 1 から。動いたら増やす（host config の pcsc_readers 件数と一致させる, 契約 §3）。
#define CCID_SLOT_COUNT 1

// ───────── USB 識別子（契約 §2: 固定 MUST）─────────
// TODO(実機): 製作時に VID/PID を確定し、契約 §2 に転記する。
//   ※ 0x303A は Espressif の VID。製品では自社/取得 VID を使う。PID はテスト用に固定でよい。
#define USB_VID 0x303A
#define USB_PID 0x8B5D  // 任意・固定。他デバイスと衝突しない値に。

// reader_name に現れる product 文字列（ファーム更新で変えない MUST, §2）。
// OS は "<product> [Interface N] ..." の体裁で描画 → host config の pcsc_readers[].name に等値で入る。
#define USB_MANUFACTURER_STR "PokerRFID"
#define USB_PRODUCT_STR      "PN5180-CCID"
#define USB_SERIAL_STR       "PKR-0001"  // device 単位で安定 (SHOULD, §2)

// ───────── PN5180 SPI（全 slot 共有バス）─────────
// ESP32-S3 用の安全な GPIO 例。USB(19/20) / strapping(0,3,45,46) / flash・PSRAM ピンを避けること。
// TODO(実機): 実配線に合わせて変更。
#define PN5180_SPI_HOST   SPI2_HOST
#define PN5180_PIN_SCK    12
#define PN5180_PIN_MOSI   11
#define PN5180_PIN_MISO   13
#define PN5180_SPI_HZ     7000000  // 7 MHz（jef-sure 例に準拠）

// ───────── PN5180 個別ピン（slot ごとに NSS/BUSY/RST）─────────
// CCID_SLOT_COUNT と同じ要素数にする。slot index はそのまま CCID slot 番号 = host の reader_name 接尾辞。
typedef struct {
    int nss;   // chip select (active low)
    int busy;  // busy line
    int rst;   // hardware reset (active low)
} pn5180_pins_t;

// TODO(実機): slot ごとの NSS/BUSY/RST を実配線に。複数台なら NSS/BUSY/RST を slot 数だけ用意。
static const pn5180_pins_t PN5180_SLOT_PINS[CCID_SLOT_COUNT] = {
    {.nss = 10, .busy = 14, .rst = 9},  // slot 0
    // {.nss = ?, .busy = ?, .rst = ?},  // slot 1 ...
};

// ───────── ポーリング間隔 ─────────
// host(RFIDThread)も polling+debounce するので、ここは RF 読取り間隔の目安。
#define CARD_POLL_INTERVAL_MS 100
