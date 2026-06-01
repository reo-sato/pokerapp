# Issue 0007: PN5180 + ESP32-S3 移行に伴う未確定事項（タグ規格 / USB-CDC transport）

## Date

2026-06-01

## Status

Open

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
3. **USB-CDC transport**: ESP32-S3 はネイティブ USB-OTG を持つ。固定卓では WiFi より
   USB-CDC シリアル接続が安定しうるが、現状 Python 側 transport は `"http"` / `"pcsc"` のみ。
   `"serial"` transport を足すかは未決定（ADR-0006 Alternative B として deferred）。
4. **クロストーク**: PN5180 の高 RF 出力により隣接席タグの誤検出が起きうる。これは
   ファーム/アンテナ側責務だが、`reader_id` マッピングの信頼性に影響する。

## Reproduction

該当なし（設計上の未確定事項。コードのバグではない）。

## Root Cause

ハードウェア仕様変更（PN532+ESP32 → PN5180+ESP32-S3）に伴う設計判断の保留。
規格選定と物理配置が運用前に固まっていないため。

## Fix

確定方針が出た時点で別タスク化する。現時点では ADR-0006 で「契約不変・UID 長可変・
dual-support」を固定し、コア実装は変更しない。dual-support を担保する回帰テスト
（8 バイト ISO15693 UID の `normalize_tag_id`）は follow-up として残す。

## Regression Test

- （予定）`tests/test_rfid.py::TestNormalizeTagId::test_iso15693_8byte_uid` — 未実装

## Affected Files

- `rfid/http_receiver.py`
- `rfid/card_master.py`
- `config_default.json`（将来 `"serial"` transport を足す場合）

## Related Worklog

- `docs/worklog/2026-06-01-rfid-hardware-migration.md`

## Related ADRs

- `docs/adr/0006-rfid-hardware-migration-pn5180-esp32s3.md`

## Related Commits

- `<commit-sha>` — RFID hardware migration docs

## Notes

PC/SC 経路（`rfid/bridge.py` / `rfid/reader_thread.py`）は ACR122U 等の USB リーダー
直結経路であり、ESP32+PN532 経路とは無関係。今回の移行の影響を受けない（混同注意）。
