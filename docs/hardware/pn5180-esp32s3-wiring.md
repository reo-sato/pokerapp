# PN5180 × 13 + ESP32-S3 制御基板 — 物理層仕様 (v2 計画書 2026-06-22 source of truth)

本ドキュメントは「ポーカーテーブル RFID システム計画書 v2 (2026-06-22)」§2 を repo の docs-as-code に
移植したもの。**物理層（GPIO 配線・電源・コネクタ）の source of truth** は本書とする。USB CCID
firmware ↔ host (PC/SC) の **論理境界** は別の正典（`docs/contracts/rfid-usb-ccid.md` v1.0 frozen +
ADR-0034）が担当し、本書はその下位レイヤを補完する。

実装の真実（firmware 側）は `firmware/esp32s3-pn5180-ccid/app_config.h`。本書と齟齬が出たら
**app_config.h が勝つ**（firmware が現場の動作を決めるため）。本書は app_config.h と整合させる。

---

## 1. 全体構成

```
[ICODE SLIX トランプ]
        ↓ かざす
[PN5180 リーダー × 13台]  (席 1..8 = 8 台 + ボード 1..5 = 5 台)
        ↓ JST XH 8 ピン × 13本（各 80〜100cm）
[制御基板（ESP32-S3 DevKitC-1 v1.0 + CD74HC4067）]
        ↓ USB ケーブル 1 本（ESP32-S3 native USB-OTG ポート）
        ↓ USB CCID class device として PC に列挙
[PC] OS PC/SC stack（pcscd / WinSCard）→ pyscard → rfid/bridge.py
```

**重要**: ESP32-S3 の **native USB-OTG ポート**を使う。UART ブリッジ IC（CP2102N / CH340 等）経由は
COM ポート（CDC）になり、PC/SC スタックに乗らない（→ CCID 認識不可）。

## 2. 主要部品

| 部品 | 型番 / 規格 | 数量 |
|---|---|---|
| マイコン | ESP32-S3 DevKitC-1 v1.0（NeoPixel @GPIO38、native USB-OTG 搭載） | 1 |
| RFID リーダー | PN5180-NFC R1.1-170710 | 13 |
| マルチプレクサ | CD74HC4067（16ch アナログ MUX） | 1 |
| 3.3V LDO | MCP1792T-3302H/CB（PN5180 各基板に個別実装） | 13 |
| ケーブル | 8 芯リボン 80〜100cm、JST XH 8 ピン | 13 本 |
| 電源 | AC アダプター 5V 4A（PSE 適合） | 1 |
| 電源変換 | DC ジャック → 端子台変換 | 1 |
| バルクコンデンサ | 1000μF / 25V 電解 | 1 |
| パスコン | 0.1μF セラミック（各 PN5180 直近） | 13+ |
| USB ケーブル | ESP32-S3 native USB ⇔ PC（USB-A or USB-C） | 1 |
| カード | ICODE SLIX 透明仕上げトランプ | 2 デッキ |

> **MCP1792T-3302H/CB を 13 個別個に置く理由**: PN5180 の RF パルスは突入電流が大きく、共通 3.3V
> から取ると相互ノイズで読み取り不安定になる。個別 LDO で電源を分離することで干渉を抑える。

## 3. GPIO 割当（**最新値**。古い設計値に注意）

### 3.1 NSS（チップセレクト）13 個別

| リーダー | reader_id (host config) | ESP32 GPIO |
|---|---|---|
| #1 席 1 | `seat_1` | **1** |
| #2 席 2 | `seat_2` | **2** |
| #3 席 3 | `seat_3` | **4** |
| #4 席 4 | `seat_4` | **5** |
| #5 席 5 | `seat_5` | **6** |
| #6 席 6 | `seat_6` | **7** |
| #7 席 7 | `seat_7` | **8** |
| #8 席 8 | `seat_8` | **9** |
| #9 ボード 1 | `board_1` | **10** |
| #10 ボード 2 | `board_2` | **15** |
| #11 ボード 3 | `board_3` | **16** |
| #12 ボード 4 | `board_4` | **17** |
| #13 ボード 5 | `board_5` | **18** |

reader_id↔役割の対応は **host config (`pcsc_readers`) が唯一の source of truth**。firmware は CCID
slot 順序を「再起動・再列挙を跨いで安定」させるだけでよく、役割名を埋め込まない。

### 3.2 SPI 共有信号（13 台共通の 1 本）

| 信号 | ESP32 GPIO |
|---|---|
| SCK | **12** |
| MOSI | **11** |
| MISO | **13** |
| RST | **14** |

- SPI clock ≤ **5 MHz**（7 MHz 以上で PN5180 不安定の実測あり）。
- Mode 0（CPOL=0, CPHA=0）, MSB first。

### 3.3 マルチプレクサ CD74HC4067（13台分の BUSY を 1 本に集約）

⚠️ **GPIO 番号が当初設計から変更されている。app_config.h の最新値を必ず使うこと**。

| 信号 | ESP32 GPIO | 備考 |
|---|---|---|
| SIG | **47** | ⚠️ 当初 GPIO 21 → strapping pin によるフローティング問題で **47** に変更 |
| S0 | **37** | ⚠️ 当初 GPIO 38 → DevKitC-1 v1.0 の NeoPixel (WS2812) 衝突で **37** に変更 |
| S1 | **39** | |
| S2 | **40** | |
| S3 | **41** | |
| VCC | **3.3V** | ⚠️ **5V 厳禁**（CD74HC4067 を破損） |
| EN | **GND 直結** | 負論理、常時有効 |

MUX チャンネル C0..C12 が PN5180 #1..#13 の BUSY に対応（C13..C15 未使用）。
リーダー N (1..13) の BUSY を読む手順:
1. S3..S0 に `(N-1)` を出力
2. 数 μs 待機（settling）
3. SIG (GPIO 47) を `digitalRead`

PN5180 の BUSY は **High=Busy / Low=Ready**（PN532 と論理が逆。要注意）。

### 3.4 ESP32-S3 で使ってはいけない / 注意が必要な GPIO

| GPIO | 理由 |
|---|---|
| 19 / 20 | native USB D-/D+（USB-OTG 使用時、他用途禁止） |
| 26 / 27 / 28 / 29 / 30 / 31 / 32 | 内蔵 PSRAM / Flash 接続（使用禁止） |
| 21 | strapping pin。起動時フローティング → 旧 MUX SIG 配置を断念した理由 |
| 38 | DevKitC-1 v1.0 の NeoPixel (WS2812) 専用 → 旧 MUX S0 配置を断念した理由 |

### 3.5 JST XH 8 ピンコネクタ pinout（13 本共通）

| ピン | 信号 |
|---|---|
| 1 | +5V |
| 2 | GND |
| 3 | SCK |
| 4 | MOSI |
| 5 | MISO |
| 6 | RST |
| 7 | NSS |
| 8 | BUSY |

## 4. USB / CCID 仕様（契約再掲）

| 項目 | 値 |
|---|---|
| USB class | **CCID (bInterfaceClass = 0x0B)** — HID / CDC / vendor 不可 |
| VID | **0x303A**（Espressif） |
| PID | **0x8B5D**（プロジェクト固定） |
| manufacturer | `PokerRFID` |
| product | `PN5180-CCID` |
| Windows reader_name | `PokerRFID PN5180-CCID 0..12`（13 slot 構成） |
| slot 数 | **13**（席 8 + ボード 5） |
| 実装ベース推奨 | **TinyUSB CCID class**（ESP-IDF v5.x の `esp_tinyusb` 経由） |

詳細・APDU 仕様は `docs/contracts/rfid-usb-ccid.md` v1.0 + ADR-0034 を参照。

## 5. RF スキャン方式（重要：時分割厳守）

**13台同時に RF ON すると相互干渉で読み取り失敗する**。必ず時分割スキャンする。

```
loop (各 slot のポーリング要求を host から受けたとき):
  1. MUX を該当 reader の BUSY に切替（S3..S0 出力、数 μs 待機）
  2. NSS LOW（該当 reader の個別 GPIO）
  3. PN5180 LOAD_RF_CONFIG
  4. PN5180 RF_ON
  5. ICODE SLIX INVENTORY
  6. UID 受信（8 バイト生バイト）
  7. PN5180 RF_OFF
  8. NSS HIGH
  9. CCID 経由で host に応答
```

**同時 RF ON は常に 1 台のみ MUST**。タイミング目標:

- 1 台あたり 5〜10 ms
- 13 台 1 サイクル 65〜130 ms
- カード認識 → host 到達レイテンシ ≤ 250 ms

> **host との責任分離**: 時分割は **firmware-internal の責務**（CCID slot ごとの `XfrBlock` 要求を
> 内部で MUX→NSS→RF_ON→INVENTORY→RF_OFF と順番に処理する）。host (`rfid/reader_thread.py`) は
> 単一スレッドで slot を順番に Get UID するだけ。OS PC/SC が CCID transfer を serialize するため
> **host を slot 並列化で高速化しない**（並列化しても firmware への到達順は変わらず無意味）。

## 6. 電源設計

- AC アダプタ 5V 4A → 端子台 → 制御基板 +5V レール
- バルクコンデンサ 1000μF / 25V を入力直後に配置（突入電流対策）
- 各 PN5180 直近に 0.1μF パスコン（ノイズ抑制）
- 各 PN5180 の 3.3V は **個別の MCP1792T-3302H/CB LDO** から供給（突入電流分散・ノイズ分離）

## 7. 変更履歴

- **2026-06-22**: 計画書 v2 で物理層 source of truth を本書（`docs/hardware/`）に確定。docs-as-code 化。
- **2026-06 (詳細日付不明)**: **MUX SIG GPIO 21 → 47** に変更。GPIO 21 が ESP32-S3 strapping pin で
  起動時フローティングし、MUX が暴れる問題への対策。
- **2026-06 (詳細日付不明)**: **MUX S0 GPIO 38 → 37** に変更。GPIO 38 が DevKitC-1 v1.0 の内蔵
  NeoPixel (WS2812) 専用で衝突するため。
- **2026-06-20**: ねふぁさん（組立業者）による LED 動作確認テストで 13 台すべて読み取り合格。
  3.3V レギュレーター（MCP1792T-3302H/CB × 13）実装完了。
- **2026-06-22**: USB CCID 版 firmware 実機 bring-up 開始。Windows PC/SC で
  `PokerRFID PN5180-CCID 0` として列挙、Status=OK。`probe_pcsc list` で見える状態に到達。

## 8. 既知の課題（実機 bring-up 残）

- (a) **BUSY timeout デバッグ中**: CCID 版 firmware で BUSY 待ちがタイムアウトする。最有力候補は
  MUX SIG/S0 の **新 GPIO（47/37）が pin_definitions.h に反映されていない**ケース。旧値（21/38）が
  残っていないか要確認（§3.3 / 9.3 参照）。
- (b) **13台密接配置のリーダー間干渉**: ねふぁさんの検証で「リーダー同士が近いと初期化で停止」報告
  あり。実テーブル埋め込み後に再検証、必要ならフェライトシート貼付で対応。ボード 5 枚密接配置も
  要確認。

## 9. 関連

- 論理境界（USB CCID firmware ↔ host PC/SC）: `docs/contracts/rfid-usb-ccid.md` v1.0
- ADR: `docs/adr/0015-pn5180-esp32s3-usb-ccid-pcsc-canonical.md` / `docs/adr/0034-rfid-usb-ccid-firmware-host-contract-freeze.md`
- 実装ベース: `firmware/esp32s3-pn5180-ccid/app_config.h`（GPIO 番号の正準）
- 実装者ガイド: `docs/rfid-ccid-firmware-checklist.md`（§8 時分割スキャン / §9 トラブルシューティング）
- 実機 QA: `docs/hardware-qa-checklist.md` + `tools/probe_pcsc.py`（list / check / watch）
