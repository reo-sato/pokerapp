# Worklog: Phase D part 3 (D2a) — rules-aware ライブ結線 + actor 競合検出

## Date

2026-06-05

## Scope / Task

v1 リリーストラック R の Phase D（GitHub issue #7）の **D2a**。`apply_corrections`（D1）と engine 境界
（D0）を `IntegrationThread` のライブ経路に結線する。誤 fold リスクが核のため、本増分は **prior 固定 +
競合検出**に留め、silent-fold 合成（`fold_through` 結線）と派生 confidence（D3）は Phase F の golden
fixtures と併走する後続に回す。統合ブランチ `v1-integration`、作業ブランチ `claude/phaseD-actor-fusion`。

## Goal

- `_handle_audio_event` のベッティング処理を `gs.legal_context()` で分岐し、rules-aware（pokerkit）では
  `apply_corrections` をライブ適用、明示発話席 / 窓内 RFID 席が手番(prior)と食い違えば `needs_review`。
- legacy（空 legal_context）は従来コードをそのまま分離して挙動不変。
- pokerkit 実走で検証、legacy 既存テストの回帰ゼロ。

## Changed Files

- `integration/engine.py`:
  - import: `apply_corrections`（runtime）, `LegalContext`（TYPE_CHECKING）。
  - `_handle_audio_event`: ベッティング処理を rules-aware / legacy へ分岐。
  - `_handle_legacy_action`（新）: 旧ベッティング処理を**逐語的に分離**（挙動不変）。
  - `_resolve_actor`（新）: prior 固定 + 競合検出（D2a。silent-fold 合成はまだ）。
  - `_handle_rules_aware_action`（新）: `apply_corrections` 適用 → `apply_action(prior, ...)` →
    ActionRecord（corrected action/amount, needs_review = 非合法 | corrected | 競合）。
- `tests/test_phase_d2_wiring.py`（新規, 5）。

## Expected Behavior

- pokerkit: "call 9999" → call 額は状態由来（heard 無視）。"check"（BB 直面）→ call + review。
  明示席が prior と不一致 → review（seat は prior 固定）。一致 → review なし。
- legacy: "bet 500" → 生 action "bet"・amount 500・seat=現手番（従来どおり）。
- 既定 legacy 経路は完全不変。

## Implemented Behavior

- 期待どおり。rules-aware/legacy の分岐は `legal_context().legal_actions` の有無で判定
  （legacy は D0 で空 context を返す stub）。
- ActionRecord.confidence は D2a では既存 `calc_confidence` を流用（3 因子融合は D3）。

## Test Results

- `python -m pytest tests/test_phase_d2_wiring.py -q` — **5 passed**（pokerkit 0.7.4 実走）。
- `python -m pytest tests/ -q --ignore=tests/test_vision.py` — **248 passed, 0 skipped**。
  既存 `test_integration` / `test_phase7`（legacy）は `_handle_legacy_action` 経由で不変通過 = 回帰なし。

## Mismatches Found During Testing

- None observed.

## Fixes Applied

- なし（新規結線 + 既存処理の逐語分離）。

## Remaining Gaps / Out-of-Scope（Phase D の残り）

- [ ] **D2b**: silent-fold 合成（`_resolve_actor` での `fold_through` 結線で prior を物理/明示証拠へ
      上書き）。状態変更を伴うため atomic 化（snapshot/restore 等）+ ISSUE-0009 の席数上限。Phase F の
      golden fixtures（`silent-fold` / `out-of-turn-rfid`）で検証。
- [ ] **D3**: 派生 confidence（3 因子 L/A/Q 合成）+ `needs_review` 5 条件。重み較正は Phase F。
- [ ] pokerkit を CI/requirements に追加（現状は検証のためローカル導入。Phase G/H で pin）。

## Related ADRs

- `docs/adr/0009-pokerkit-live-rules-authority.md`（§1 data flow / §4 actor / §5 corrections）

## Related Issues

- `docs/issues/0009-actor-conflict-silent-fold-policy.md`（D2a で競合「検出」、合成は D2b）
- GitHub issue #7（Phase D）/ Epic #4。

## Related Commits

- （本タスクの commit を後で追記）
