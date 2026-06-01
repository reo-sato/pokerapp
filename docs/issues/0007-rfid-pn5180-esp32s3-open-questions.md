# Issue 0007: PN5180 + ESP32-S3 移行に伴う未確定事項（タグ規格 / USB-CDC transport）

## Date

2026-06-01

## Status

Partially Resolved（USB-CDC transport は実装済 / タグ規格・クロストークは Open）

## Severity / Priority

- Severity: Low
- Priority: P3

## Area

rfid / docs / hardware boundary

## Expected Behavior

RFID ハードウェアを PN5180 + ESP32-S3 に移行する（ADR-0006）にあたり、運用前に
確定しておきたい設計上の open question を 1 箇所に記録しておく。HTTP JSON transport
契約自体は ADR-0006 で「不変」と確定済み。

## Actual Behavior

以下が未確定のまま:

1. **タグ規格（ISO14443A / ISO15693）**: 採用規格は **未定**。方針は「両対応にしたい」
   （dual-support）。PN5180 は ISO15693（8 バイト UID）を読めるが、実際に ICODE 系
   vicinity タグを使うかは未決定。
2. **UID エンコード順**: ISO15693 を使う場合、UID の MSB/LSB 順がファーム実装に依存する。
   `tag_id` 文字列がどのバイト順で来るかをファームと突き合わせる必要がある。
3. **USB-CDC transport**: ✅ **Resolved** — USB 直結運用が確定したため `"serial"` transport を
   実装した（ADR-0007）。`rfid/serial_receiver.py::RFIDSerialReceiver`、既定 transport を
   `serial` に変更。改行区切り JSON / 自動再接続。
4. **クロストーク**: 🔲 **Open** — PN5180 の高 RF 出力により隣接席タグの誤検出が起きうる。
   これはファーム/アンテナ側責務だが、`reader_id` マッピングの信頼性に影響する。

## Reproduction

該当なし（設計上の未確定事項。コードのバグではない）。

## Root Cause

ハードウェア仕様変更（PN532+ESP32 → PN5180+ESP32-S3）に伴う設計判断の保留。
規格選定と物理配置が運用前に固まっていないため。

## Fix

- **USB-CDC transport**: USB 直結確定を受けて `"serial"` transport を実装（ADR-0007）。
  `rfid/serial_receiver.py` 新設、`rfid/event_builder.py` に受信ロジックを共通化、
  既定 transport を `serial` に変更。
- **dual-support**: 8 バイト ISO15693 UID の正規化を回帰テストで担保（下記）。
- **タグ規格 / UID エンコード順 / クロストーク**: 引き続き Open。規格確定・実機検証は
  ハードウェア入手後に別タスク化する。

## Regression Test

- ✅ `tests/test_rfid_serial.py::TestSerialReceiverRobustness::test_iso15693_8byte_uid_normalized`
  — ISO15693 8 バイト UID が `RFIDEvent.tag_id` まで正しく正規化される dual-support 回帰テスト。

## Affected Files

- `rfid/serial_receiver.py`（新規, USB-CDC 受信）
- `rfid/event_builder.py`（新規, transport 共通ロジック）
- `rfid/http_receiver.py`
- `rfid/card_master.py`
- `config_default.json`

## Related Worklog

- `docs/worklog/2026-06-01-rfid-hardware-migration.md`
- `docs/worklog/2026-06-01-rfid-usb-cdc-serial-transport.md`

## Related ADRs

- `docs/adr/0006-rfid-hardware-migration-pn5180-esp32s3.md`
- `docs/adr/0007-rfid-usb-cdc-serial-transport.md`

## Related Commits

- `<commit-sha>` — RFID hardware migration docs

## Notes

PC/SC 経路（`rfid/bridge.py` / `rfid/reader_thread.py`）は ACR122U 等の USB リーダー
直結経路であり、ESP32+PN532 経路とは無関係。今回の移行の影響を受けない（混同注意）。
