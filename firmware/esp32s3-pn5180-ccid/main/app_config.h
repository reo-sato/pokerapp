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
// **本番は 11 台**（席 8 + board 3）。board は 1 台 1 枚ではなく「重ね置き」で運用する:
//   board1 = flop 3 枚重ね / board2 = turn 1 枚 / board3 = river 1 枚。
// 席 reader も hole card 2 枚重ね。したがって **1 reader で複数カードを読む**必要があり、
// inventory は anti-collision（mask DFS, 下の PN5180_MAX_CARDS_PER_READER）で行う。
// 配線表 PN5180_READERS は 13 台分あるが、本番で使うのは **先頭 11**（#12/#13 は予備）。
//
// まず 1 台で MUX+SPI 経路を検証 → 動いたら 11 に上げる。
//
// 【段階手順（1 変数ずつ動かす）】
//   1) CCID_SLOT_COUNT=1（bring-up）: 通電中の MUX ch から reader を自動選択（どのコネクタでも可）。
//   2) CCID_SLOT_COUNT=2: ch0/ch1 = reader #1/#2 を配線して 2 台同時。ここで RF 時分割
//      （PN5180_RF_OFF_BETWEEN_READERS）と slot↔物理の対応が効いているかを見る。
//   3) CCID_SLOT_COUNT=11: 本番全台。=2 以上では自動選択をせず PN5180_READERS の配列順 = slot 順。
//   各段階で host 側 `python tools/probe_pcsc.py list` の reader 件数 = slot 数、`watch` で
//   「どの slot にかざすとどの席/board が出るか」を確認する（役割は host config が source of truth）。
//   PN5180_SPI_HZ の 1MHz→5MHz は **11 台が 1MHz で安定してから** 単独で上げる（同時に変えない）。
// slot 数を変えたら usb_descriptors.c の bcdDevice も連動して変わる（Windows の記述子キャッシュ対策）。
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

// 13 台分（先頭 CCID_SLOT_COUNT 個だけ有効化）。配列順 = CCID slot 順（slot 0.. = #1.. =
// host config の pcsc_readers 順）。**本番は先頭 11**（slot 0..7 = 席 1..8、slot 8/9/10 =
// board1 flop 3 枚 / board2 turn / board3 river）。#12/#13 は予備で通常未使用。
// reader #N の BUSY = MUX channel (N-1)（docs/hardware/pn5180-esp32s3-wiring.md §3 と一致）。
// 物理対応が違う場合は実配線に合わせる。
//
// 【bring-up（CCID_SLOT_COUNT=1）】pn5180_reader.c が起動時の MUX scan で「通電中の ch」を見つけ、
// その ch の reader（nss）を自動選択して init する。1 台だけ繋ぐ検証で、どのコネクタに挿しても
// 再ビルド不要（実機で ch12 → ch7 に変わって init 失敗した反省）。全 ch floating なら [0] を使う。
static const pn5180_reader_cfg_t PN5180_READERS[] = {
    {.nss = 1,  .mux_ch = 0},   // #1  席 1
    {.nss = 2,  .mux_ch = 1},   // #2  席 2
    {.nss = 4,  .mux_ch = 2},   // #3  席 3
    {.nss = 5,  .mux_ch = 3},   // #4  席 4
    {.nss = 6,  .mux_ch = 4},   // #5  席 5
    {.nss = 7,  .mux_ch = 5},   // #6  席 6
    {.nss = 8,  .mux_ch = 6},   // #7  席 7
    {.nss = 9,  .mux_ch = 7},   // #8  席 8
    {.nss = 10, .mux_ch = 8},   // #9  ボード 1（flop 3 枚重ね）
    {.nss = 15, .mux_ch = 9},   // #10 ボード 2（turn 1 枚）
    {.nss = 16, .mux_ch = 10},  // #11 ボード 3（river 1 枚）— 本番はここまで（11 slot）
    {.nss = 17, .mux_ch = 11},  // #12 予備
    {.nss = 18, .mux_ch = 12},  // #13 予備
};

// ───────── ポーリング間隔 ─────────
// 1 周（全 slot を 1 回ずつ読む）の後に待つ時間。11 台では 1 周そのものが長くなるので、実測
// （下の POLL_STATS_INTERVAL_MS で出る「poll 統計」ログ）を見て調整する。PRESENCE_HOLD_MISSES
// （pn5180_reader.c）は **サイクル数** なので、1 周が伸びるとカード離脱の判定時間も同じ比率で伸びる。
#define CARD_POLL_INTERVAL_MS 100

// ───────── poll 周期の計測ログ ─────────
// この間隔（ms）ごとに「1 周の min/avg/max・最長 reader・ready slot 数」を INFO で出して統計を
// リセットする。11 台化したときに 1 周が何 ms かかるか（= カード検出の遅れ）を実測するための計測。
// 0 で無効（計測コードごと除外）。
#define POLL_STATS_INTERVAL_MS 10000

// ───────── RF 時分割（同時に磁界を張るのは 1 台だけ）─────────
// 1 = 各 reader の inventory 直後に pn5180_setRF_off() を呼ぶ（既定）。
//   理由: jef-sure ドライバの get_all_uids() は内部で setupRF → inventory を行うが **RF を ON の
//   まま戻る**。11 台を順に読むと全台の磁界が同時 ON になり、(a) 隣接アンテナ同士の干渉で
//   inventory が不安定になる、(b) 電流が台数ぶん積み上がる（USB バスパワーでは危険）。
//   ドライバ README も "Toggle RF off/on between scans … allow 5.1 ms for tags to return to IDLE"
//   を推奨している（次にその reader を読むのは 1 周後 = CARD_POLL_INTERVAL_MS 以上あとなので足りる）。
// 0 = 従来挙動（RF は ON のまま）。1 台構成での A/B 比較・切り分け用。
#define PN5180_RF_OFF_BETWEEN_READERS 1

// ───────── CCID slot の「カード有無」の見せ方 ─────────
// 1 = 仮想カード常時挿入（既定）。slot を常に present として IccPowerOn に必ず ATR を返し、物理
//     カードの有無は Get UID の SW だけで伝える（あり: UID + 90 00 / なし: 6A 81）。
//     理由: Windows(usbccid) のカード有無追跡が当てにならなかった（実機 2026-09-10: interrupt
//     通知は無視され、polling でも一度 MUTE(0x80100066) を latch すると slot 状態が更新されず
//     power-on を再試行しない）。host(rfid/bridge.py) は SW≠90 00 を「カード無し」と扱い、
//     RFIDThread が None↔UID の遷移で debounce する（契約 §6/§8）ので、OS のスロット状態機械に
//     依存せずに hot-plug が成立する。
// 0 = 物理カードの有無をそのまま slot 状態に反映（pcsc-lite など polling が信頼できる環境向け）。
#define CCID_VIRTUAL_CARD_ALWAYS_PRESENT 1

// ───────── 試行するカード規格 ─────────
// 本番カードは ICODE SLIX（ISO 15693, 8B UID）のみ。ISO 14443A も毎 poll で試すと、カード無しの
// 間 REQA/anticollision のタイムアウト（数百 ms）で poll が 1 周 ~800ms に落ち、ログも
// `Timeout waiting for anticollision` で埋まる（実機 2026-09-10）。Mifare 等 14443A を使う検証の
// ときだけ 1 にする。
#define PN5180_TRY_ISO14443 0

// ───────── 高速 inventory（自前の mask anti-collision, ISSUE-0021）─────────
// 1 = pn5180_reader.c の自前経路（既定）。ISO15693 INVENTORY を **1 slot（1 応答）** で送り、
//     衝突したときだけ mask を 1 bit ずつ伸ばす DFS で複数枚を分離する。RF ON は reader ごと 1 回。
// 0 = ドライバの proto->get_all_uids()（堅牢だが遅い）。A/B 比較・切り分け用。
//
// 【なぜ自前経路が要るか（実測）】
//   実機 1 台での「poll 統計」= 1 周 min/avg/max が 176/365/839 ms、361/726/890 ms、419/701/860 ms
//   （10 秒窓 × 3、カード無し・有りを含む）。11 台に増やすと 1 周 2〜10 秒になり実用不可。
//   原因は jef-sure ドライバの get_all_uids() が毎回
//     RF 設定 2 種（ASK10% → ASK100%）× データレート 2 種（high → low）＝ 最大 4 回の inventory
//     （各 40ms timeout）＋ 衝突 DFS ＋ jitter リトライ（esp_random で 5〜15ms sleep）
//   を総当たりするため（src/pn5180-15693.c の get_all_uids / inventory_single_slot）。しかも
//   「見つかった rf_config で break」しないので、カードがあっても 2 種類の RF 設定を必ず両方走る。
//   本番カードは ICODE SLIX（ISO 15693, 8B UID）1 種だけなので、総当たりは不要。
// 目標: **カード無しの reader 1 台あたり ≤ 15 ms**（= 11 台で 1 周 ≈ 200 ms）。
//
// 注: fast 経路は ISO15693 専用。PN5180_TRY_ISO14443 は **fast=1 のとき無視**される
//     （14443 を試したいときは PN5180_FAST_INVENTORY=0 にしてドライバ経路に戻す）。
#define PN5180_FAST_INVENTORY 1

// fast 経路が LOAD_RF_CONFIG に渡す TX 設定（pn5180-15693.h の enum）。
//   PN5180_15693_26KASK100 = 0x0D（既定） / PN5180_15693_26KASK10 = 0x0E
// ⚠ ASK100 を既定にする理由: ドライバの pn5180_loadRFConfig() は **RX 設定を `tx | 0x80` で決め打ち**
//   する（src/pn5180.c:816）。PN5180 の RF config 表では 0x0D→0x8D = ISO15693 **26 kbps** RX、
//   0x0E→0x8E = **53 kbps** RX なので、標準 INVENTORY（high data rate = 26.48 kbps 応答）を
//   受けられるのは **0x0D/0x8D の組だけ**。ドライバは ASK10 → ASK100 を必ず両方試すので
//   「ドライバで読めている＝ASK10 で読めている」ではない（成功しているのは ASK100 の回と考えられる）。
//   ASK10 は電源が安定する利点があるので、実機で ASK100 の届きが悪ければここを
//   PN5180_15693_26KASK10 にして A/B する（読めなくなったら RX 設定の不一致が原因）。
#define PN5180_FAST_RF_CONFIG PN5180_15693_26KASK100

// INVENTORY 1 回あたりの応答待ちの上限。ISO15693 の応答は t1(≈320µs) + 10 byte @26.48kbps ≈ 4ms
// で来るので 10ms で十分。カード無しの reader はこの時間だけ待って「無し」と判定する
// （= 1 周の下限を決める値。probe 1 回 ≈ この値）。
#define PN5180_FAST_RX_TIMEOUT_MS 10

// RF ON 後、タグが給電されて応答できるようになるまでの待ち（ISO/IEC 15693-3: VCD は磁界確立から
// 1ms 待ってから要求を送る）。reader ごと DFS の最初に 1 回だけ。
#define PN5180_FAST_FIELD_SETTLE_US 1000

// ───────── 1 reader に重ねて置くカード枚数（anti-collision）─────────
// 席 reader = hole card 2 枚重ね、board1 = flop 3 枚重ね、board2/3 = 1 枚。余裕を見て 4。
// cache（pn5180_card_t.uids）と CCID Get UID の連結応答（count × 8B + SW）もこの数で決まる。
// ⚠ 増やすときは ccid_device.c の in_buf（10 + 64）と bulk EP の 64 byte を超えないこと
//   （4 枚 = 10 + 4*8 + 2 = 44 byte でまだ余裕がある。7 枚超で 64 を割るのでチェイン送信が必要）。
#define PN5180_MAX_CARDS_PER_READER 4

// 1 reader / 1 poll あたりの INVENTORY 送信回数の上限（= その reader の所要時間の上限）。
// DFS は衝突するたびに枝を 2 本に割るので、上限が無いとノイズで 1 台が数百 ms を食う。
// 目安: カード無し 1 回 / 2 枚 4〜6 回 / 3 枚 6〜9 回。打ち切っても取れた分だけ返し、
// 残りは次の poll（と host 側 debounce）が拾う。
#define PN5180_FAST_MAX_PROBES 12
