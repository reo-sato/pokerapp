# Worklog: RFID ハードウェア移行 (PN532+ESP32 → PN5180+ESP32-S3) のドキュメント整合

## Date

2026-06-01

## Scope / Task

RFID センサーのハードウェア仕様変更（PN532 + ESP32 → PN5180 + ESP32-S3）に伴う
設計計画の修正とドキュメント整合。コードロジックは変更しない（ADR-0006）。

## Goal

- 「ハードウェアが変わっても HTTP JSON transport 契約は不変」という設計判断を ADR 化する。
- CLAUDE.md / docstring の PN532・ESP32(-WROOM-32) 記述を PN5180・ESP32-S3 に整合させる。
- タグ規格未定（ISO14443A / ISO15693 dual-support 希望）と USB-CDC transport 候補を
  open question として記録する。
- decision-log / CHANGELOG に索引・ユーザー可視変更を追記する。

## Changed Files

- `docs/adr/0006-rfid-hardware-migration-pn5180-esp32s3.md` — 新規 ADR（ハード移行 + 契約不変）
- `docs/issues/0007-rfid-pn5180-esp32s3-open-questions.md` — 新規 issue（タグ規格 / USB-CDC / クロストーク）
- `CLAUDE.md` — プロジェクト概要 / ディレクトリ構成 / 技術スタック / エラーハンドリング方針の RFID 記述更新
- `integration/engine.py` — docstring の "ESP32" → "ESP32-S3"（ロジック変更なし）
- `rfid/http_receiver.py` — docstring / コメントの "ESP32(-WROOM-32)" → "ESP32-S3"、契約不変の注記追加（ロジック変更なし）
- `docs/decision-log.md` — ADR-0006 / ISSUE-0007 を index に追加
- `CHANGELOG.md` — Unreleased に RFID ハード移行を追記

## Expected Behavior

- ハードウェア交換が Python コアのロジック・テストに波及しないことを ADR で明文化。
- ISO14443A（4/7 バイト）/ ISO15693（8 バイト）UID が `normalize_tag_id` でそのまま扱える
  ことを契約上の dual-support として記録。
- アプリ挙動・API・テスト結果に変化がない（ドキュメントと docstring のみの更新）。

## Implemented Behavior

期待どおり。RFID transport の受信ロジック（`rfid/http_receiver.py`）・カード解決
（`rfid/card_master.py`）・統合（`integration/engine.py`）の実行コードは無変更。
docstring とドキュメントのみ更新した。

## Test Results

- `pytest` — 本環境では未インストールのため未実行。
- `python -m py_compile rfid/http_receiver.py integration/engine.py rfid/card_master.py` — OK
  （編集は docstring のみで構文有効、実行ロジック無変更のため既存テストの挙動は不変）。

## Mismatches Found During Testing

None observed.（ドキュメント整合タスクのため挙動変化なし）

## Fixes Applied

- なし（mismatch なし）。

## Remaining Gaps / Out-of-Scope

- [ ] 8 バイト（ISO15693）UID の `normalize_tag_id` 回帰テスト追加（ISSUE-0007）。
- [ ] ISO15693 採用可否・UID エンコード順の確定（ISSUE-0007）。
- [ ] ESP32-S3 ネイティブ USB を活かす `"serial"` transport の要否判断・実装（ISSUE-0007, ADR-0006 Alt.B）。
- [ ] PN5180 高 RF 出力に伴うクロストーク対策（ファーム/アンテナ側責務）。

## Related ADRs

- `docs/adr/0006-rfid-hardware-migration-pn5180-esp32s3.md` — ハード移行 + transport 契約不変

## Related Issues

- `docs/issues/0007-rfid-pn5180-esp32s3-open-questions.md` — タグ規格 / USB-CDC / クロストーク

## Related Commits

- `<commit-sha>` — RFID hardware migration docs (PN5180 + ESP32-S3)
