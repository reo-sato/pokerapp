# Worklog: Phase F part 3 (F3b) — hand / action schema freeze（ISSUE-0011 Fixed）

## Date

2026-06-05

## Scope / Task

v1 リリーストラック R の Phase F（GitHub issue #8）の **F3b**（R5 schema freeze）。hand core の `hand`
（= `HandSummary`）/ `action`（= `ActionRecord`）契約を実ファイル化して `1.0` に freeze し、ISSUE-0011 を
決着する。統合ブランチ `v1-integration`、作業ブランチ `claude/phaseF-schema-freeze`。F3 の残り PHH 区別
（F3c）は後続。

## Goal

- `docs/contracts/schemas/{hand,action}.schema.json` を実ファイル化（`version 1.0`）。
- fixtures + `_MODELS` 登録 + code↔contract + golden→schema テスト。
- ISSUE-0011 の open question（additionalProperties 方針・required 集合・ADR-0008 統合）を確定。

## Changed Files

- `docs/contracts/schemas/hand.schema.json` / `action.schema.json`（新規, `1.0`, additionalProperties:true）。
- `docs/contracts/fixtures/{hand,action}/`（canonical / valid-minimal / invalid-*）。
- `tests/test_contracts.py`: `_MODELS` に `hand`/`action` 追加 + `test_core_hand_action_match_contract`。
- `tests/test_reconstruction.py`: `test_golden_output_conforms_to_hand_action_schema`（5 ケース）。
- docs: ISSUE-0011（Fixed）/ decision-log / hand-reconstruction.md §7 / CHANGELOG / CLAUDE.md / 本 worklog。

## Decisions（ISSUE-0011）

- **additionalProperties: true** のまま `1.0`（`false` 化は後続、範囲膨張防止）。
- **required**: `action` は常時出力 12 フィールド、`hand` は cross-app 安定 core 6。`pots`（ADR-0009）/
  `player_id`（ADR-0008）/ `committed` 等の additive は **optional**（legacy/fallback で absent/null 可）。
- `players[i].player_id` を UUID4 hex pattern・nullable で `hand` に optional 同居（S2.x 後も additive 互換）。

## Expected vs Implemented

- schema↔fixture: canonical/valid-minimal 通過、invalid-*（action 不正 enum / hand session_id 欠落）違反。
- code↔contract: `HandSummary.to_dict()` / `ActionRecord.to_dict()` が schema 適合。
- golden→schema: 5 green ケースの**非正規化**再構築出力（実 timestamp・`pots`・合成 fold 含む）が適合。

## Test Results

- `pytest tests/test_contracts.py tests/test_reconstruction.py -q` — **31 passed**。
- `pytest tests/ -q --ignore=tests/test_vision.py` — **282 passed, 0 skipped**（回帰なし）。

## Mismatches Found During Testing

- なし。normalized golden（`started_at=null` 等）は schema を通らないため、golden→schema は **非正規化の実
  出力**を検証する設計にした（normalize は replay 比較専用）。

## Fixes Applied

- なし（新規 schema + fixtures + テスト）。

## Remaining Gaps / Out-of-Scope

- [ ] **F3c**: PHH の call/check 区別（`output/phh_exporter.py` で両方 "cc"）。
- [ ] `additionalProperties:false` 昇格（v1 後）。重み最終較正・pokerkit pin（G/H）。

## Related ADRs / Issues

- ADR-0010（R5 freeze）/ ADR-0008（player_id additive）/ ADR-0009（pots/committed additive）。
- **ISSUE-0011 Fixed**。GitHub issue #8（Phase F）/ Epic #4。

## Related Commits

- （本タスクの commit を後で追記）
