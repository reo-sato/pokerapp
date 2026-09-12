# Worklog: PN5180 複数 reader（最大 13 台）への拡張準備（firmware）

## Date

2026-09-10（同日の 1 slot bring-up `2026-09-10-rfid-ccid-end-to-end-bringup.md` の続き）

## Scope / Task

`firmware/esp32s3-pn5180-ccid/main/` を **`CCID_SLOT_COUNT` を 1 から 13 へ上げられる状態**にする
準備。実機での 13 台検証（Phase H）そのものは対象外で、13 台にしたときに必ず問題になる 4 点
（RF の重なり / 未通電 reader / 1 周の所要時間 / Windows の記述子キャッシュ）を先に潰す。
契約 `docs/contracts/rfid-usb-ccid.md` v1.0（ADR-0034 / ADR-0040）は据え置き（§2 に additive 追記のみ）。

## Goal

- `CCID_SLOT_COUNT` の値（1 / 2 / 13）に依らず正しくコンパイル・動作する firmware にする。
- 13 台を順に読んでも **同時に RF 磁界を張るのは 1 台だけ**にする。
- 1 台挿し忘れても残りで起動できる（未通電 reader を skip）。配線ミスが起動ログで分かる。
- 13 台化したときの **1 周の所要時間を実測**できる（カード検出の遅れの見積り）。
- slot 数を変えたら Windows が記述子を確実に読み直す。
- コミット時点の `CCID_SLOT_COUNT` は **1 のまま**（挙動不変で、実機 1 slot の回帰を起こさない）。

## Changed Files

firmware（`firmware/esp32s3-pn5180-ccid/`）:

- `main/app_config.h` — `PN5180_RF_OFF_BETWEEN_READERS`（既定 1）/ `POLL_STATS_INTERVAL_MS`（既定 10000）
  を追加。`CCID_SLOT_COUNT` に **段階手順（1→2→13、SPI 1MHz→5MHz は最後に単独で）** のコメント。
  `CARD_POLL_INTERVAL_MS` に「13 台では poll 統計ログの実測を見て調整。`PRESENCE_HOLD_MISSES` は
  サイクル数なので 1 周が伸びると離脱判定も伸びる」を追記。
- `main/pn5180_reader.c` —
  (a) **RF 時分割**: 各 reader の inventory 直後に `pn5180_setRF_off()`（`rf_off_after_read`）。失敗は
  reader ごと最初の 3 回だけ WARN、以降 DEBUG（static カウンタ）。
  (b) **未通電 reader の skip（部分成功）**: `CCID_SLOT_COUNT > 1` のとき、起動時 MUX scan の
  `low_mask` に該当 ch が無い reader は `pn5180_init` を呼ばずに `dev=NULL` で skip。init 失敗時は
  「共有 SPI device が解放されたので全停止」と明記して `return false`（`ready_count == 0` のときだけ
  `diag_after_init_failure`）。ループ後に `PN5180 ready: N/M slot（skip: …）` の要約。
  併せて **配線チェック**（`diag_wiring_map`）: 設定 ch なのに floating / 通電しているのに設定範囲外、
  を列挙し、全一致なら `配線 OK`。`_Static_assert` で `CCID_SLOT_COUNT` ≤ `PN5180_READERS` 要素数。
  (c) **poll 周期の計測**: `esp_timer_get_time()` で 1 周と各 reader の所要時間を測り、
  `POLL_STATS_INTERVAL_MS` ごとに `poll 統計(直近 N 周): 1 周 min/avg/max = …, 最長 reader #k …, ready N slot`
  を INFO で出して統計をリセット。`poll_once` は `dev == NULL` の slot を skip。
- `main/usb_descriptors.c` — (d) `bMaxCCIDBusySlots` を **`0x01` 固定**（実装は 1 コマンドずつ処理）。
  `bcdDevice` を **`0x0200 | CCID_SLOT_COUNT`** に（slot 数変更 = `bMaxSlotIndex` 変更 = 記述子変更 →
  Windows の VID/PID/REV キャッシュを必ず無効化する）。履歴 0x0100/0x0101/0x0102 はコメントに残置。
- `main/CMakeLists.txt` — `REQUIRES` に `esp_timer` を追加（`esp_timer_get_time` 用。main が `REQUIRES` を
  明示しているため「全コンポーネント自動要求」の既定が外れており、明示しないと `esp_timer.h` 未解決になり得る）。

docs:

- `firmware/esp32s3-pn5180-ccid/README.md` — 「残 TODO」を実装済/未検証で書き直し、「13 台化の段階手順」
  を追加。既知の制限の旧名 `PN5180_SLOT_PINS` → `PN5180_READERS` に修正し、**ドライバ制約**
  （`pn5180_init` 失敗 = 共有 SPI 解放 = 全停止 / 個別 `pn5180_deinit` 禁止）を明記。
- `docs/rfid-ccid-firmware-checklist.md` — 新節 **§8 複数 slot（13 台）** + 受け入れマトリクスに
  「slot 数 = `probe_pcsc list` 件数」の行。
- `docs/contracts/rfid-usb-ccid.md` — §2 に 1 文 additive（slot 数変更も `bcdDevice` を変える MUST /
  firmware は `0x0200 | slot 数`）。**version は 1.0 のまま据え置き**。
- `CHANGELOG.md` / `CLAUDE.md`（実装状況表の firmware 行の「残」）/ 本 worklog。

## 根拠（jef-sure/pn5180 ドライバの事実 — 設計判断の前提）

managed component のためリポジトリには入っていないが、public ヘッダと実装から確認した事実:

1. **SPI device は全 reader で 1 本の共有**。`pn5180_spi_init(host, sck, miso, mosi, hz)` が
   `spics_io_num = GPIO_NUM_NC` で device を 1 つだけ add し、`pn5180_init(spi, nss, busy, rst)` は
   各 reader がその共有ハンドルを使う（NSS は driver が `gpio_set_level` で手動駆動）。
2. **`pn5180_init` の失敗経路は共有 SPI device を解放する**。reset 失敗 / EEPROM の firmware version が
   読めない・不足 のいずれでも `pn5180_deinit(ret, false)` を呼び、その中で
   `spi_bus_remove_device(共有ハンドル)` が走る。
   → **「失敗した 1 台だけ skip して続行」は不可能**（以降どの reader も SPI が使えない）。個別 reader に
   `pn5180_deinit` を呼ぶのも同じ理由で禁止。だから未通電の台は **init を呼ぶ前に**弾く必要がある。
   なお `pn5180_init` は内部で共有 RST を pulse する（全チップがリセットされる）が、RF 設定は毎回
   `get_all_uids` の `setupRF` で再ロードされるので実害はない。
3. **`get_all_uids()` は RF を ON のまま戻る**。内部で `setupRF`（`is_rf_on` なら off → loadRFConfig → on）
   → inventory を RF 設定 2 種 × データレート 2 種で最大 4 回試行するが、戻るときに RF を切らない。
   → 13 台を順に読むと全台の磁界が ON のままになる（干渉 + 電流）。ドライバ README も
   "Toggle RF off/on between scans (`pn5180_setRF_off()`/`pn5180_setRF_on()`) and allow 5.1 ms for tags to
   return to IDLE" を推奨。次に同じ reader を読むのは 1 周後（≥ `CARD_POLL_INTERVAL_MS`）なので 5.1ms は満たす。
4. public API に `bool pn5180_setRF_off(pn5180_t *)` があり、`pn5180_t` は public に全フィールド定義。

また **Windows は USB 記述子を VID/PID/bcdDevice でキャッシュする**（実機で確認済: EP 構成を変えたとき
bcdDevice を上げないと反映されなかった）。`bMaxSlotIndex` は `CCID_SLOT_COUNT-1` なので、slot 数変更は
記述子変更 = REV を変える必要がある → `bcdDevice` を slot 数に連動させた。

## Expected Behavior

- `CCID_SLOT_COUNT` が 1 / 2 / 13 のいずれでも警告なくコンパイルでき、**1 のときの挙動は従来と同一**
  （bring-up 自動選択・診断ログ・poll 経路）。
- `CCID_SLOT_COUNT > 1` の起動時:
  - 設定 ch と通電 ch の突き合わせ結果が出る（不一致は WARN で列挙、一致なら `配線 OK`）。
  - 未通電の reader は `→ skip` の WARN が出て、`PN5180 ready: N/M slot（skip: #…）` で起動する。
  - 通電しているのに `pn5180_init` が失敗したら、共有 SPI 解放の理由を出して **全停止**（`return false`）。
  - 1 台も ready でなければ ESP_LOGE して `return false`。
- poll では skip した slot を触らず（host からは Get UID が常に `6A 81`）、各 reader の inventory 直後に
  RF を off にする。10 秒ごとに 1 周の min/avg/max と最長 reader が INFO で出る。
- `bcdDevice` が 1 slot で `0x0201`、13 slot で `0x020D`。`bMaxCCIDBusySlots` は常に 1。

## Implemented Behavior

上記のとおり実装。**この container には ESP-IDF toolchain が無いためファームの実ビルドは未実施**。
代わりに ESP-IDF / ドライバ API のスタブヘッダを書き、host の gcc 13 で
`-std=gnu17 -Wall -Wextra` の **フルコンパイル**（`-fsyntax-only` ではなく `-c`）を
`pn5180_reader.c` / `usb_descriptors.c` / `ccid_slot.c` に対して実施:

- `CCID_SLOT_COUNT` × `POLL_STATS_INTERVAL_MS` × `PN5180_RF_OFF_BETWEEN_READERS` ×
  `PN5180_BUSY_VIA_MUX` × `PN5180_TRY_ISO14443` の 9 通りで **警告 0 / エラー 0**。
- `CCID_SLOT_COUNT` = 0 / 14 では `_Static_assert` が意図どおりビルドを止める。
- 記述子の実値をダンプ: 1 slot → `bcdDevice=0x0201 bMaxSlotIndex=0 bMaxCCIDBusySlots=1`、
  2 → `0x0202/1/1`、13 → `0x020D/12/1`、`wTotalLength=86`（EP 構成は不変）。

さらに GPIO/SPI/PN5180 をシミュレートするハーネスに **実物の `pn5180_reader.c` をリンク**して
ロジックを実行（実機ではない）:

| シナリオ | 結果 |
|---|---|
| 13 slot / ch0-11 通電 + ch15 に想定外 | `配線チェック: … floating … = #13(ch12)` / `… 設定範囲外の ch = ch15(表に無い)` / `reader #13 … → skip` / `PN5180 ready: 12/13 slot（skip: #13）` / init=true / slot12 は present=0 |
| 13 slot / 全通電 | `配線 OK: 設定 13 ch すべて通電` / `PN5180 ready: 13/13 slot` |
| 2 slot / ch0,ch1 + ch7 も通電 | `… 設定範囲外の ch = ch7(=#8) → CCID_SLOT_COUNT(2) を増やすか配線表を確認` / 2/2 ready |
| 13 slot / 全通電だが #3 が応答しない | reader 0,1 ready の後 `pn5180_init reader 2 failed` + 共有 SPI 解放の説明 → init=false（深掘り診断は ready>0 なので出さない） |
| 13 slot / 1 台も通電なし | 全台 skip → `PN5180 ready 0 台（…）` → init=false |
| 1 slot / ch7 だけ通電（設定 ch0） | 従来どおり `bring-up: … ch7 → reader #8 (nss=GPIO9) を自動選択` → 1/1 ready |
| poll ×120 周 | `poll 統計(直近 75 周): 1 周 min/avg/max = …, 最長 reader #1 (slot 0) = … , ready 12 slot` |

## Test Results

- `pytest tests/ -q --ignore=tests/test_vision.py` → **577 passed, 44 skipped**（26.5s）。
  skip はこの container に optional 依存（pokerkit / fastapi / jsonschema 等）が入っていないためで、
  CI（`requirements-dev.txt`）では skip 0。**本タスクは Python を 1 行も触っていない**（firmware + docs のみ）。
- firmware: 上記のスタブ・フルコンパイル（9 構成 × 3 ファイル、警告 0）+ シミュレータ実行。
- **未実施**: ESP-IDF v5.3.5 での実ビルド、実機での書き込み・13 台検証（ユーザー環境のタスク）。

## Mismatches Found During Testing

1. `_Static_assert` のメッセージに日本語を入れたところ、gcc の診断が非 ASCII を 8 進エスケープして
   `\37777777742…` と読めなくなった（ESP-IDF のビルドログでも同じになる）。
2. `-fsyntax-only` では **ファイルスコープ static の未使用警告が出ない**（コンパイル終了時の解析を
   行わないため）ことが分かり、最初の「警告 0」判定が甘かった。
3. `pn5180_reader.c` は `free()` を呼ぶが `<stdlib.h>` を include していない（**既存**。ESP-IDF では
   FreeRTOS.h 経由で入るためビルドは通っており、実機でも動作実績あり）。

## Fixes Applied

1. `_Static_assert` のメッセージを ASCII 固定にし、理由をコメントで残した。
2. 検証を `-c`（フルコンパイル）に切り替え、ダミーの未使用変数・書式不一致を仕込んで
   「警告がちゃんと出る」ことを確認したうえで 9 構成を再実行した。
3. 既存かつ実機で動作しているため **本タスクでは触らない**（無関係な差分を増やさない）。将来
   firmware を触るときに `#include <stdlib.h>` を足すのが望ましい。

## Remaining Gaps / Out-of-Scope

- [ ] **13 台の実機検証（段階 1→2→13）**: 各段階で `probe_pcsc list` の件数 = slot 数、`watch` で各 slot が
      再発火、`poll 統計` ログで 1 周の実測。RF 時分割・未通電 skip・配線チェックはすべて **実機未検証**。
- [ ] **`PN5180_SPI_HZ` 1MHz → 5MHz**（13 台が 1MHz で安定してから、単独で変更）。
- [ ] **host 側の 1 周時間**: firmware の poll 統計とは別に、`rfid/reader_thread.py`（`RFIDThread`）が
      13 slot を 1 周するのに何 ms かかるかは未計測。firmware が速くても host が遅ければ検出が遅れる。
- [ ] **電源**: 現在の記述子は `bMaxPower = 0x32`（100mA）でバスパワー宣言。13 台の PN5180 は
      RF 時分割込みでも 100mA を超える可能性が高い。実測して `bMaxPower` を実態に合わせるか、
      外部電源にする（外部電源なら `bmAttributes` の self-powered も検討）。
- [ ] `CARD_POLL_INTERVAL_MS` / `PRESENCE_HOLD_MISSES` の 13 台向け調整（実測後）。
- [ ] `rfid_cards.json` へのカード登録（`tools/register_cards.py`）と host config の
      `pcsc_readers` 13 件化。

## Related ADRs

- `docs/adr/0040-ccid-virtual-card-always-present.md` — slot は仮想カード常時挿入、カード有無は
  Get UID の SW だけで伝える。未通電で skip した slot が「常に `6A 81`」で成立するのはこの設計のため。
- `docs/adr/0034-rfid-usb-ccid-firmware-host-contract-freeze.md` — 契約 v1.0 の凍結。本タスクの §2 追記は additive
  （version 据え置き）。

## Related Issues

- `docs/issues/0015-pn5180-usb-ccid-firmware-contract.md` — firmware↔host 契約（Fixed）。本タスクは契約変更なし。

## Related Commits

- （本 worklog と同一コミット）firmware: RF 時分割 / 未通電 reader skip + 配線チェック / poll 統計 /
  `bcdDevice` の slot 数連動 / `bMaxCCIDBusySlots=1`、および docs 一式。
