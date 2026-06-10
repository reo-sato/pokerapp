# Worklog: RFID hardware migration — PN5180 + ESP32-S3 (planning, docs-only)

## Date

2026-06-01

## Scope / Task

RFID 読み取りハードウェアを PN532 + ESP32 から **PN5180 + ESP32-S3** に切り替える仕様変更が
降りた件について、**設計方針の確定と docs 反映のみ** を行う。コード・config・テストの実改修は
別タスクに分離する。

## Goal

- 方針 ADR（ADR-0014）を Accepted で起こす。
- firmware ↔ Python の API 契約固定を ISSUE-0014 で追跡開始する。
- CLAUDE.md（プロジェクト概要 / 技術スタック / 実装状況 / ディレクトリ docstring）を新ハードに追従。
- 既存コードは触らない（PCSC 経路含め、判断は ADR にのみ記録）。

## Changed Files

- `docs/adr/0014-migrate-rfid-to-pn5180-esp32s3-and-canonical-http-transport.md` — 新規。
  HTTP transport を canonical、PCSC を legacy 降格、JSON 契約は不変。
- `docs/issues/0014-pn5180-firmware-http-contract.md` — 新規。firmware ↔ Python API 契約の
  open question を登録。
- `CLAUDE.md` — RFID 関連の以下を更新:
  - § プロジェクト概要: `ESP32 + PN532` → `ESP32-S3 + PN5180`。
  - § ディレクトリ構成: `reader_thread.py` の説明に **legacy** 注記。
  - § 技術スタック: PN5180 行追加 / PC/SC 行に legacy 注記。
  - § 実装状況: RFID PC/SC 行を legacy 化、PN5180 移行を planned 行で明示。
  - § エラーハンドリング方針: ESP32-S3 への呼称統一。
- `CHANGELOG.md` — Docs / Planning に RFID hardware migration を追記。
- `docs/decision-log.md` — ADR-0014 / ISSUE-0014 を Index に追加。

## Expected Behavior

- 既存コード・config・テストは **一切変更しない**（docs-only コミット）。
- 既存テストはすべて従来通り通る。
- ADR-0014 が「HTTP canonical / PCSC legacy / JSON 契約不変 / UID 4-7-8B 許容」を明文化。
- CLAUDE.md 上で PN5180 + ESP32-S3 が「現時点で採用予定の RFID 構成」として読める。
- ISSUE-0014 が `tag_id` 書式・8B UID 検証・error/heartbeat 等の未確定事項を register する。

## Implemented Behavior

期待どおり実装:

- ADR-0014 Accepted（Alternatives A/B/C で両系統維持・JSON 即時変更・PCSC 即時削除を却下、
  HTTP canonical + PCSC legacy 降格を採用）。
- ISSUE-0014 Open（firmware 仕様未確定 5 項目をリスト）。
- CLAUDE.md / CHANGELOG / decision-log を更新。
- コード・config・tests に対する変更は **無し**（意図的）。

## Test Results

- `python -m pytest tests/test_contracts.py tests/test_player_repository.py
  tests/test_player_registry_gui.py -q` → **32 passed**（既存 baseline 維持、回帰なし）。
- 注: hand logger 系テスト（test_rfid* 等）は本環境に `numpy` / `customtkinter` 未導入のため
  collection 不可。本タスクは production code 不変のため影響なし。

## Mismatches Found During Testing

None observed。docs-only のため挙動回帰の余地が無い。

## Fixes Applied

- なし。

## Remaining Gaps / Out-of-Scope

- [ ] `card_master.normalize_tag_id` の 8B UID 許容確認 + 単体テスト追加（**ADR-0014 follow-up**）。
- [ ] `rfid/reader_thread.py` / `rfid/bridge.py` docstring と `config_default.json` コメントに
      「PN5180 では非対応」を明示する小改修（次タスク）。
- [ ] `tests/test_rfid_http.py` への 8B UID fixture 追加。
- [ ] PN5180 firmware（別 repo）の API 契約サンプル payload を ISSUE-0014 に貼って固定。
- [ ] PCSC 経路の完全削除可否判断（別 ADR）。
- [ ] 将来: `docs/contracts/schemas/rfid_event.schema.json`（POST /rfid body の contract 化）。

## Related ADRs

- `docs/adr/0014-migrate-rfid-to-pn5180-esp32s3-and-canonical-http-transport.md` — 本タスクの中心判断。

## Related Issues

- `docs/issues/0014-pn5180-firmware-http-contract.md` — firmware ↔ Python 契約の未確定事項。

## Related Commits

- 本 worklog と同じコミット（RFID hardware migration planning, docs-only）。
