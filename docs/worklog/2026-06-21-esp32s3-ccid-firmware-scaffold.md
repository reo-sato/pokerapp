# Worklog: ESP32-S3 + PN5180 USB CCID firmware scaffold

## Date

2026-06-21

## Scope / Task

本番 RFID（canonical USB CCID → PC/SC, ADR-0015/0034）の device firmware を ESP-IDF で scaffold する。
実機 bring-up で「現状 firmware は LED 点灯テストのみ＝PC へ未送出」と判明し、host 側は完成済みのため、
不足する firmware を実装する（operator 選択: ESP-IDF / この repo の `firmware/`）。

## Goal

- ESP32-S3 が PN5180 を **USB CCID smart card reader** として公開し、host の `tools/probe_pcsc.py` /
  hand logger がそのまま UID を読める firmware の土台を作る。
- 契約 `docs/contracts/rfid-usb-ccid.md` v1.0 / `docs/rfid-ccid-firmware-checklist.md` の MUST を
  ファイルに対応付け、実機で埋める箇所を明示する。

## Changed Files

- `firmware/esp32s3-pn5180-ccid/CMakeLists.txt` / `sdkconfig.defaults` / `.gitignore` — ESP-IDF プロジェクト。
- `firmware/.../main/CMakeLists.txt` / `idf_component.yml` — ビルド + 依存（esp_tinyusb / jef-sure pn5180）。
- `firmware/.../main/app_config.h` — slot 数 / SPI ピン / VID-PID / product 文字列（実機で編集）。
- `firmware/.../main/usb_descriptors.[ch]` — CCID USB 記述子（class 0x0B, bulk IN/OUT, §2）。
- `firmware/.../main/ccid_device.[ch]` — TinyUSB カスタムクラス登録 + bulk plumbing（版依存）。
- `firmware/.../main/ccid_slot.[ch]` — **CCID メッセージ処理の中核**（ATR §5 / Get UID §6 / UID §7）。
- `firmware/.../main/pn5180_reader.[ch]` — PN5180 で UID 読取り + カード状態キャッシュ。
- `firmware/.../main/main.c` — USB 起動 + PN5180 ポーリングタスク。
- `firmware/.../README.md` — ビルド/フラッシュ手順 + TODO + 契約 §↔ファイル対応 + host 受け入れ。
- `CLAUDE.md`（ディレクトリ構成 + 実装状況行）/ `CHANGELOG.md` / `docs/issues/0015-...md`。

## Expected Behavior

- 焼くと native USB に CCID デバイスが現れ、Windows「スマートカード読み取り装置」/ `probe_pcsc list` に
  product=`PN5180-CCID` の reader_name が出る。`check` で connect PASS、`watch` でタップ時に UID 表示。

## Implemented Behavior

- 上記を満たす **scaffold**。設計上のキモ:
  - **USB(版依存) と CCID プロトコル(契約準拠) を分離**: `ccid_slot.c:ccid_process_message` を
    framework 非依存の純関数にし、`ccid_device.c` は bulk の出し入れに限定。USB 版差で揺れても契約核は不変。
  - **USB と RF を分離**: `pn5180_reader.c` が周期ポーリングで slot ごとのカード状態をキャッシュし、
    CCID 層はそれを読むだけ（host の RFIDThread と同じ polling/debounce 思想）。
  - Get UID（`FF CA 00 00 00`→UID+`90 00`、カード無し→`6A 81`）を host `rfid/bridge.py` の期待に一致させた。
  - PC/SC-3 storage-card proxy ATR を固定（host は ATR 非依存 §5）。UID は生バイトで返し host が正規化（§7）。

## Test Results

- `python -m pytest tests/ --ignore=tests/test_vision.py -q` — **701 passed**（firmware は C で Python
  CI に影響せず。host 側 RFID/probe テストも緑のまま）。
- firmware 自体のビルド（`idf.py build`）/ フラッシュ / USB 列挙は **実機 + ESP-IDF 環境が必要**で本環境では
  未実施（scaffold の宣言どおり）。

## Mismatches Found During Testing

- 本環境に ESP-IDF / ESP32-S3 が無いため firmware のコンパイル検証は不可。USB CCID クラス記述子・
  `usbd_class_driver_t` 構成・esp_tinyusb / jef-sure の API は **版/実機依存**で、ビルド時に調整が要る
  （README と各 `TODO(実機)` に明示）。

## Fixes Applied

- USB 初期化を esp_tinyusb 方式（`tinyusb_driver_install` に記述子を渡す）に統一し、`tud_descriptor_*_cb`
  の自前定義をやめて重複シンボルを回避（`usb_descriptors.c`）。

## Remaining Gaps / Out-of-Scope

- [ ] 実機で `idf.py build` → フラッシュ → `probe_pcsc list/check/watch` の通し検証。
- [ ] `app_config.h` のピン / VID-PID、`pn5180_reader.c` の jef-sure 実 API、`ccid_device.c` の
      `usbd_class_driver_t` 構成、`main.c` の `tinyusb_config_t` フィールドを実環境に合わせる。
- [ ] 確定した VID/PID・実 reader_name を契約 `rfid-usb-ccid.md` §2/§4 に転記（ISSUE-0015）。
- [ ] CCID 64byte 超チェイン受信 / live hot-add は scaffold 時点で未対応（契約上も hot-add は future）。

## Related ADRs

- `docs/adr/0015-...md`（PC/SC canonical）/ `docs/adr/0034-...md`（契約 v1.0 freeze）。

## Related Issues

- `docs/issues/0015-pn5180-usb-ccid-firmware-contract.md`（firmware 実装 = 実環境残）。

## Related Commits

- 本ワークログと同じコミット（firmware scaffold 追加）。
