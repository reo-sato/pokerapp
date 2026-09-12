# Worklog: Phase 0b / S2 planning — session / seat_assignment / hand_ref contract draft

## Date

2026-05-25

## Scope / Task

S2（session + hand-based seating）の本実装に先立ち、`session` / `seat_assignment` /
`hand_ref` の contract を **draft（未 freeze）** として contract-first で整備し、`hand_id` の
cross-app boundary（ISSUE-0004）を決着する。production code には踏み込まない。

## Goal

- `session` / `seat_assignment` / `hand_ref` の draft schema + fixtures + 契約文書を追加。
- `hand_id` の cross-app 参照形を確定（ADR）。
- freeze 対象 / 未凍結事項 / blockers を明文化。
- repository / service interface 草案を追加。
- contract test を新 model に拡張し緑を維持。
- docs-as-code（CLAUDE.md / CHANGELOG / worklog / issue / decision-log）を更新。

## Changed Files

- `docs/adr/0006-s2-session-seating-contract-and-hand-id-cross-app-reference.md` — 新規。hand_id の
  cross-app 参照を `(session_id, hand_id)` 複合キー（int 据え置き）に確定。Alternatives A/B/C。
- `docs/contracts/session-seating.md` — 新規。S2 モデル定義 / hand_id boundary / interface 草案 /
  freeze 状態・blockers。
- `docs/contracts/schemas/session.schema.json` — 新規（v0.1, draft）。
- `docs/contracts/schemas/seat_assignment.schema.json` — 新規（v0.1, draft）。
- `docs/contracts/schemas/hand_ref.schema.json` — 新規（v0.1, draft）。
- `docs/contracts/fixtures/{session,seat_assignment,hand_ref}/*.json` — 新規。canonical /
  valid-minimal / invalid-*（各 model）。
- `docs/contracts/repository-interfaces.md` — session/seating interface 草案を追記。
- `docs/contracts/shared-ids.md` — session_id（ISSUE-0005 参照）/ hand_id（ADR-0006 で reconcile 済）を更新。
- `docs/contracts/error-shapes.md` — S2 error code を additive 追記。
- `docs/contracts/versioning-and-freeze.md` — freeze order #3 の状態を draft に更新。
- `docs/contracts/README.md` — ディレクトリ構造に新 schema/fixtures と session-seating.md を反映。
- `docs/issues/0004-hand-id-int-vs-cross-app-string.md` — Resolved（ADR-0006）に更新。
- `docs/issues/0005-s2-session-seating-freeze-blockers.md` — 新規。freeze 前の open question 登録。
- `docs/decision-log.md` — ADR-0006 / ISSUE-0005 追加、ISSUE-0004 を Resolved に。
- `CLAUDE.md` — Phase 0b 完了状況 / Phase 2 契約状況 / freeze order を追記。
- `CHANGELOG.md` — Docs / Planning (Phase 0b) を追記。
- `tests/test_contracts.py` — `_MODELS` に session / seat_assignment / hand_ref を登録。

## Expected Behavior

- 新 3 schema が valid JSON（draft 2020-12）で `$id` + `version` を持つ。
- 各 model の `canonical` / `valid-minimal` は schema を通過し、`invalid-*` は必ず違反する。
- contract test が 3 model を追加しても緑のまま。
- 既存 S1 / player contract test に回帰がない。
- production code（core/gui）は不変。

## Implemented Behavior

期待どおり:

- `session`（session_id / started_at / status 必須、label / ended_at / blinds 任意、
  player membership は持たず seating から導出）。
- `seat_assignment`（hand-based の 1 行、`(session_id, hand_id, seat_no)` キー、partial 許容、
  status で sitting_out）。
- `hand_ref`（`(session_id, hand_id)` 複合キー、started_at、seat_assignments 埋め込み snapshot）。
- `hand_id` は int 据え置き、cross-app は複合キー（ADR-0006、選択肢 A）。
- contract test の `_MODELS` に 3 model を追加し schema↔fixture 整合を検証。

## Test Results

- `python -m pytest tests/test_contracts.py tests/test_player_repository.py
  tests/test_player_registry_gui.py -q` → **32 passed**
  （S1 の 29 に対し +3 = session / seat_assignment / hand_ref の `test_fixtures_match_schema`）。
- 注: hand logger 系テスト（test_gui / integration / parser / phase7 / rfid*）は本環境に
  `numpy` / `customtkinter` 未導入のため collection 不可。S2 は production code を触らないため影響なし。

## Mismatches Found During Testing

- 初回 `pytest` で 4 件失敗。原因は **新規作成した全ファイルの末尾に `</content>`（一部 ADR は
  `</invoke>` も）が混入**していたこと（書き込み時のアーティファクト）。JSON parse error として顕在化。

## Fixes Applied

- 全該当ファイルから末尾の `</content>` / `</invoke>` 行を除去し、再実行で 32 passed を確認。
  混入残無しを `grep` で確認済み。

## Remaining Gaps / Out-of-Scope

- [ ] **ISSUE-0005**: `session_id` 最終採番方式 / seat_assignment 永続形 / mid-session seat change
      表現。これらが freeze（schema 1.0 昇格）の blocker。
- [ ] S2 core 実装（`core/session*.py` 等）と `code↔contract` test（player 相当）は本タスク out of scope。
- [ ] desktop / mobile の session UI、ledger / point / settlement（S3〜）、sync / API（S5）。
- [ ] schema は draft（v0.x）。`1.0` 昇格 = freeze は S2 着手時。

## Related ADRs

- `docs/adr/0006-s2-session-seating-contract-and-hand-id-cross-app-reference.md` — 本タスクの中心判断。
- `docs/adr/0003-...`（hand_ref 概念定義）、`docs/adr/0005-...`（contracts layout / freeze workflow）。

## Related Issues

- `docs/issues/0004-hand-id-int-vs-cross-app-string.md` — Resolved（ADR-0006）。
- `docs/issues/0005-s2-session-seating-freeze-blockers.md` — 新規 Open（freeze blockers）。

## Related Commits

- 本 worklog と同じコミット（S2 contracts planning, Phase 0b）。
