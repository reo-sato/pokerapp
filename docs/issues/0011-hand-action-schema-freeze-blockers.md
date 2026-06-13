# Issue 0011: hand / action schema の freeze（1.0 昇格）未確定事項

## Date

2026-06-03

## Status

<!-- One of: Open / Investigating / Fixed / WontFix / Duplicate -->
Fixed（2026-06-05, Phase F3b / #8。`hand` / `action` を `1.0` で freeze）

## Severity / Priority

- Severity: Medium（draft 段階では着手可。`1.0` freeze 前に確定が必要。ISSUE-0005 の hand core 版）
- Priority: P2

## Area

contracts / hand-reconstruction (hand / action)

## Expected Behavior

ADR-0010 の `hand`（= `HandSummary`）/ `action`（= `ActionRecord`）契約が freeze 済（schema `1.0` +
fixtures + contract test 緑 + ADR Accepted, `docs/contracts/versioning-and-freeze.md` の freeze 定義）になり、
core / desktop / mobile / replay が同一契約で本実装に入れる状態。

## Actual Behavior

`docs/contracts/hand-reconstruction.md` §7 に **draft sketch（version `0.x`, `additionalProperties:true`）**が
あるのみ。freeze に必要な以下が **未確定**:

1. **`additionalProperties:false` 昇格の可否**: 既存 `HandSummary.to_dict()`（`core/hand_log.py:57`）は
   `board` / `board_source` / `pot_total` / `winner_seat` / `review_required` 等を持ち、`action` も
   `pot_after` / `stack_after` / `source` 等を持つ。`false` で締めるには **既存の全フィールドを契約に
   取り込む**必要があり範囲が大きい（ADR-0008 §8.1 が `true` に留めた理由と同じ）。
2. **additive 新フィールドの必須/optional 確定**: ADR-0009 の `pots` / `committed`、同じく ADR-0009 の
   `legal_actions` / `amount_to_call` / `corrected_from` / `actor_source` / `asr_confidence` をどこまで
   required にするか（legacy / fallback では absent/null）。
3. **ADR-0008 との統合**: `players[i].player_id`（ADR-0008 additive）と本 ADR の additive を同一 `hand`
   schema に同居させる版管理（version 昇順・互換性）。

## Reproduction

仕様レビュー（freeze 前の open question）:

1. `docs/contracts/hand-reconstruction.md` §7 の sketch を参照。
2. `core/hand_log.py` の `HandSummary.to_dict()`（:57）/ `ActionRecord.to_dict()`（:23）の実フィールドを確認。
3. `docs/contracts/hand-integration.md` §8.1（ADR-0008 の HandSummary draft sketch）と整合を確認。

## Root Cause

hand core は contract-first 以前から有機的に育っており（多数フィールド・`additionalProperties` 前提なし）、
そこへ複数 ADR（0008/0009/0010）の additive を載せる。既存形の取り込み範囲と版管理が未整理。

## Fix

**Fixed（2026-06-05, Phase F3b / #8）。確定事項**:

1. **`additionalProperties` 方針**: ロードマップ推奨どおり **`true` のまま `1.0`** に昇格（`false` 化は
   後続、範囲膨張防止）。既存の多数フィールドを全列挙せず、安定 core を required に絞る。
2. **required 集合**:
   - `action`: 常時出力の 12 フィールド（`hand_id`/`timestamp`/`street`/`seat`/`player_name`/`action`/
     `amount`/`pot_after`/`stack_after`/`source`/`needs_review`/`confidence`）。additive 推定
     （`legal_actions`/`corrected_from`/`actor_source`/`asr_confidence` 等）は optional。
   - `hand`: cross-app 安定 core 6（`hand_id`/`session_id`/`started_at`/`ended_at`/`players`/`actions`）。
     `pots`（ADR-0009 additive）/ `player_id`（ADR-0008 additive）/ `committed` 等は **optional**
     （legacy/fallback で absent/null 可）。
3. **ADR-0008 統合**: `players[i].player_id`（UUID4 hex pattern, nullable）を `hand` schema に optional で
   同居。S2.x 接続後も additive で互換。

実装: `docs/contracts/schemas/{hand,action}.schema.json`（`version 1.0`）、
`docs/contracts/fixtures/{hand,action}/`（canonical / valid-minimal / invalid-*）、`test_contracts.py` の
`_MODELS` に登録 + `test_core_hand_action_match_contract`（code↔contract）、`test_reconstruction.py` の
`test_golden_output_conforms_to_hand_action_schema`（golden 5 ケースの実出力が schema 適合）。

## Regression Test

- freeze 時: `tests/test_contracts.py`（schema↔fixture）+ code↔contract テスト。
- `tests/test_reconstruction.py`（golden replay の `expected_hand.json` が `hand` schema を通る, ADR-0010 §5）。

## Affected Files

- `docs/contracts/hand-reconstruction.md` §7
- `core/hand_log.py`（`HandSummary` / `ActionRecord`）
- `docs/contracts/schemas/`（将来 `hand.schema.json` / `action.schema.json`）
- `tests/test_contracts.py`（`_MODELS` 拡張先）

## Related Worklog

- `docs/worklog/2026-06-03-rules-aware-reconstruction-planning.md`

## Related ADRs

- `docs/adr/0010-contract-first-hand-core-record-replay.md`
- `docs/adr/0008-hand-logger-session-integration-strategy.md`（`players[i].player_id` additive と同居）

## Related Commits

- 本 issue と同じコミット（reconstruction engine design planning）

## Notes

ISSUE-0005（S2 session/seat/hand_ref の freeze blockers）の hand core 版。draft のまま着手し、R4/R5 で
順次クローズする。
