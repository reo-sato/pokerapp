# Worklog: PN5180 + ESP32-S3 — PCSC canonical pivot (supersede ADR-0007)

## Date

2026-06-01

## Scope / Task

直前のタスクで起こした ADR-0007（「HTTP を canonical、PCSC を legacy」）が user の意図に反する
誤決定だった。**正しくは PC/SC 経路が本筋** で、ESP32-S3 は USB CCID として PN5180 ×N を公開
する。ADR-0008 を立てて ADR-0007 を Superseded に倒し、CLAUDE.md / decision-log / issue を
新方針に追従させる。docs-only。

## Goal

- ADR-0008 を Accepted で起こし、Decision・Alternatives・Consequences を整理する。
- ADR-0007 を Superseded（Status + Superseded by 行）に倒す。本文は書き換えない（history 温存）。
- ISSUE-0006（HTTP 契約）を Superseded、ISSUE-0007（USB CCID 契約）を Open で起こす。
- CLAUDE.md（プロジェクト概要 / ディレクトリ / 技術スタック / 実装状況 / エラーハンドリング）を
  「PC/SC canonical / HTTP optional secondary」に flip。
- CHANGELOG / decision-log を ADR-0008 / ISSUE-0007 / supersession に追従。
- production code は不変。

## Changed Files

- `docs/adr/0008-pn5180-esp32s3-usb-ccid-pcsc-canonical.md` — 新規 Accepted。ESP32-S3 USB CCID
  multi-slot で PN5180 ×N を PC/SC として公開、pyscard を canonical。
- `docs/adr/0007-migrate-rfid-to-pn5180-esp32s3-and-canonical-http-transport.md` — Status を
  **Superseded by ADR-0008** に更新（本文は書き換えない）。
- `docs/issues/0007-pn5180-usb-ccid-firmware-contract.md` — 新規 Open。USB descriptors /
  reader_name / ATR / pseudo-APDU / 8B UID / hot-plug 通知を register。
- `docs/issues/0006-pn5180-firmware-http-contract.md` — Status を **Superseded by ISSUE-0007** に更新。
- `CLAUDE.md` — RFID 関連 5 箇所（概要 / ディレクトリ / 技術スタック / 実装状況 /
  エラーハンドリング）を PC/SC canonical に flip。
- `CHANGELOG.md` — 直前の Docs / Planning エントリを訂正版に置き換え（ADR-0007 訂正含む）。
- `docs/decision-log.md` — ADR-0008 / ISSUE-0007 追加、ADR-0007 / ISSUE-0006 を Superseded に更新。

## Expected Behavior

- ADR-0007 が history として残り、Status に「Superseded by ADR-0008」と back-link が付く。
- ADR-0008 が「PC/SC canonical / HTTP optional secondary」を明文化し、ISSUE-0007 で USB CCID
  firmware 契約を追跡する。
- CLAUDE.md / docstring は新方針と一貫している。production code には触らない。
- 既存テストは従来通り通る。

## Implemented Behavior

期待どおり実装:

- ADR-0008 Accepted（Alternatives A/B/C/D で B〜D を却下、A 採用。D は ADR-0007 の案を明示的に却下）。
- ADR-0007 を Superseded に倒し、Supersedes/Superseded by を相互リンク。
- ISSUE-0007 Open、ISSUE-0006 Superseded。
- CLAUDE.md / CHANGELOG / decision-log を flip。
- production code・config・tests への変更は **無し**（意図的）。

## Test Results

- `python -m pytest tests/test_contracts.py tests/test_player_repository.py
  tests/test_player_registry_gui.py -q` → **32 passed**（既存 baseline 維持、回帰なし）。
- 注: hand logger 系テスト（test_rfid* 等）は本環境に `numpy` / `customtkinter` 未導入のため
  collection 不可。docs-only のため影響なし。

## Mismatches Found During Testing

- 上流タスク（ADR-0007）で **作業者が PC/SC 経路を「legacy」と誤想定**していた。これは user の
  「pc/sc 経路は本筋」という方針提示で顕在化。本タスクで supersession として訂正。
- 教訓: hardware boundary や transport の優先順位は、推測ではなく明示的に確認してから ADR を
  起こす。今回は AskUserQuestion で USB CCID topology を確定してから ADR-0008 を起こした。

## Fixes Applied

- ADR-0008 で方針を訂正、ADR-0007 を Superseded に倒し、関連 docs を flip。
- ISSUE 系列も ISSUE-0006 → ISSUE-0007 へ pivot。

## Remaining Gaps / Out-of-Scope

- [ ] `rfid/reader_thread.py` / `rfid/bridge.py` の docstring と `config_default.json` のコメントを
      「PCSC が canonical（USB CCID 経由）」に書き直す小改修（次タスク）。
- [ ] `card_master.normalize_tag_id` の 8B UID 許容確認 + 単体テスト追加。
- [ ] `tests/test_rfid.py` に PN5180 想定（8B UID）の PCSC bridge fixture を追加。
- [ ] ESP32-S3 firmware の USB CCID descriptors / reader_name / ATR を ISSUE-0007 に貼って固定。
- [ ] 将来: PC/SC reader_name 命名規約 / pseudo-APDU セットの `docs/contracts/` 化（任意）。

## Related ADRs

- `docs/adr/0008-pn5180-esp32s3-usb-ccid-pcsc-canonical.md` — Accepted。
- `docs/adr/0007-migrate-rfid-to-pn5180-esp32s3-and-canonical-http-transport.md` — Superseded。

## Related Issues

- `docs/issues/0007-pn5180-usb-ccid-firmware-contract.md` — Open。
- `docs/issues/0006-pn5180-firmware-http-contract.md` — Superseded。

## Related Commits

- 本 worklog と同じコミット（PCSC canonical pivot, docs-only）。
