// app_config.h — 実機（13台 PN5180 + CD74HC4067 MUX で BUSY 集約）の設定。
//
// アーキテクチャ（実機）:
//   - SCK/MOSI/MISO/RST は 13 台共通（直結）。NSS は reader 個別。
//   - BUSY 13 本は CD74HC4067（16ch アナログ MUX）に入り、S0-S3 で 1 本を選んで SIG に集約。
//     ESP32 は SIG(=PN5180_PIN_BUSY_SIG) を読む。各 reader を処理する前に MUX channel を切替える
//     （pn5180_reader.c の mux_select）。
//   - 契約 docs/contracts/rfid-usb-ccid.md v1.0 / docs/rfid-ccid-firmware-checklist.md。
#pragma once

#include <stdint.h>
#include "driver/spi_master.h"

// ───────── CCID slot 数（= 有効化する PN5180 台数）─────────
// まず 1 台で MUX+SPI 経路を検証 → 動いたら 13 に上げる（PN5180_READERS は 13 台分定義済み）。
#define CCID_SLOT_COUNT 1

// ───────── USB 識別子（実機確定値, 契約 §2）─────────
#define USB_VID 0x303A
#define USB_PID 0x8B5D
#define USB_MANUFACTURER_STR "PokerRFID"
#define USB_PRODUCT_STR      "PN5180-CCID"
#define USB_SERIAL_STR       "PKR-0001"

// ───────── PN5180 SPI（全 reader 共有バス）─────────
#define PN5180_SPI_HOST   SPI2_HOST
#define PN5180_PIN_SCK    12
#define PN5180_PIN_MOSI   11
#define PN5180_PIN_MISO   13
#define PN5180_SPI_HZ     1000000   // bring-up は 1MHz まで落として SI 余裕を取る。動いたら 5MHz 復帰

// ───────── PN5180 共有制御線 ─────────
#define PN5180_PIN_RST    14        // RST は 13 台共通（実機配線）

// ───────── BUSY は CD74HC4067 MUX 経由（13 本 → 1 本に集約）─────────
// jef-sure ドライバには busy = PN5180_PIN_BUSY_SIG を渡し、各 reader の処理前に MUX channel を
// 切り替えて「選択中 reader の BUSY」を SIG に出す。
#define PN5180_PIN_BUSY_SIG 47      // MUX SIG → ESP32 入力（選択中 reader の BUSY）
#define MUX_PIN_S0  37              // ※ 38(NeoPixel) 回避で 37（PSRAM 無効前提なら使用可）
#define MUX_PIN_S1  39
#define MUX_PIN_S2  40
#define MUX_PIN_S3  41
// MUX EN は GND 直結（常時有効）= ハード側。MUX VCC = 3.3V（5V 禁止）。

// ───────── BUSY 読み取り方式（切り分け用フラグ）─────────
// 1 = MUX 経由（本番。上の BUSY_SIG / MUX_* を使う）。
// 0 = 直結（MUX をバイパス。reader #1 の BUSY を PN5180_PIN_BUSY_DIRECT に直接配線して検証）。
//     → これで「MUX が原因」か「PN5180/SPI/RST/電源 が原因」かを切り分けられる。
#define PN5180_BUSY_VIA_MUX     1
#define PN5180_PIN_BUSY_DIRECT  21  // bypass 時に reader #1 BUSY を直結する空き GPIO

// ───────── 各 reader の NSS と、BUSY が繋がる MUX channel ─────────
typedef struct {
    int nss;      // chip select (active low)
    int mux_ch;   // この reader の BUSY が入っている MUX channel (0..15)
} pn5180_reader_cfg_t;

// 13 台分（先頭 CCID_SLOT_COUNT 個だけ有効化）。reader #N の BUSY = MUX channel (N-1) と仮定。
// ※ 物理 BUSY→MUX channel の対応が違う場合は mux_ch を実配線に合わせる。
//
// 【bring-up 順序】CCID_SLOT_COUNT=1 のとき [0] のリーダーで検証する。実機の MUX scan で
// 通電中のリーダーが ch12(=#13) と判明したため #13 を先頭に置く。動いたら配列順を元に戻し
// CCID_SLOT_COUNT=13 に上げる。
static const pn5180_reader_cfg_t PN5180_READERS[] = {
    {.nss = 18, .mux_ch = 12},  // [0]=#13（bring-up 検証中、現在通電中）
    {.nss = 1,  .mux_ch = 0},   // #1
    {.nss = 2,  .mux_ch = 1},   // #2
    {.nss = 4,  .mux_ch = 2},   // #3
    {.nss = 5,  .mux_ch = 3},   // #4
    {.nss = 6,  .mux_ch = 4},   // #5
    {.nss = 7,  .mux_ch = 5},   // #6
    {.nss = 8,  .mux_ch = 6},   // #7
    {.nss = 9,  .mux_ch = 7},   // #8
    {.nss = 10, .mux_ch = 8},   // #9
    {.nss = 15, .mux_ch = 9},   // #10
    {.nss = 16, .mux_ch = 10},  // #11
    {.nss = 17, .mux_ch = 11},  // #12
};

// ───────── ポーリング間隔 ─────────
#define CARD_POLL_INTERVAL_MS 100
