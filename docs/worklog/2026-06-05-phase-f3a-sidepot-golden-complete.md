# Worklog: Phase F part 2 (F3a) — side-pot 連携 + golden 5 ケース全緑

## Date

2026-06-05

## Scope / Task

v1 リリーストラック R の Phase F（GitHub issue #8）の **F3a**（R5 の side-pot 連携部分）。pokerkit が
算出する main/side pot を `HandSummary` に additive 連携し、最後の pending golden ケース `unequal-allin`
を緑化する。これで **既知バグ 5 ケースが全緑（DoD #2）**。統合ブランチ `v1-integration`、作業ブランチ
`claude/phaseF-sidepot-freeze`。F3 の残り（schema freeze / PHH 区別）は後続。

## Goal

- `HandSummary.pots` を additive 追加（legacy は `[]`、挙動不変）。
- `unequal-allin` fixture を authoring し緑化（main/side pot を検証）。
- 全 golden 5 ケース緑 + 回帰ゼロ。

## Changed Files

- `core/hand_log.py`: `HandSummary.pots: list = field(default_factory=list)`（`to_dict` に含む）。
- `integration/engine.py:_finalize_hand`: `pots=gs.pots()`（`PokerEngine.pots()`、pokerkit は end_hand 時に
  `state.pots` から算出済、legacy は `[]`）。
- `tests/fixtures/reconstruction/unequal-allin/`（新規 setup/events/expected）+ 既存 4 fixtures 再凍結
  （`pots` フィールド追加。manual winner 終了のため pots=[]）。
- `tests/test_reconstruction.py`: GREEN_CASES に `unequal-allin` 昇格、`PENDING_CASES={}`、
  `unequal_allin_main_and_side_pots` / `pending_cases_all_green` テスト追加。
- docs: CHANGELOG / event-replay.md §5 / CLAUDE.md / 本 worklog。

## Expected vs Implemented

- `unequal-allin`（3 players 1000/3000/3000、seat3 all-in → seat1 call 900[=all-in] → seat2 call 2800）:
  `pots = [{amount:3000, eligible:[1,2,3]}, {amount:4000, eligible:[2,3]}]`（合計 7000）。短スタック seat1 は
  side pot から除外。各アクションは clean（review なし、conf 0.532）。
- 既存 4 fixtures は manual winner 終了で `st.pots` 未形成のため `pots=[]`（pot_total は従来どおり）。

## Test Results

- `pytest tests/test_reconstruction.py -q` — **16 passed**（golden 5 全緑、round-trip 決定性、side-pot 検証）。
- `pytest tests/ -q --ignore=tests/test_vision.py` — **274 passed, 0 skipped**
  （`pots` は additive のため test_logger / test_phh_exporter / legacy は無改修で通過 = 回帰なし）。

## Mismatches Found During Testing

- `pots` 追加で golden 4 fixtures の full-dict 比較が落ちた（期待どおり）→ 再凍結で解消。他テストは
  additive のため影響なし。

## Fixes Applied

- なし（additive フィールド + fixture authoring）。

## Remaining Gaps / Out-of-Scope（F3 の残り）

- [ ] **F3b**: `hand` / `action` schema を実ファイル化（`docs/contracts/schemas/`、`pots` 含む）、
      `test_contracts.py` の `_MODELS` 登録、code↔contract テスト、**ISSUE-0011 決着**
      （`additionalProperties` 方針）。
- [ ] **F3c**: PHH の call/check 区別（`output/phh_exporter.py` で両方 "cc" になっている問題）。
- [ ] 重み（confidence / cap）の最終較正。pokerkit を CI/requirements に pin（G/H）。

## Related ADRs / Issues

- ADR-0009 §3（pots/committed）/ ADR-0011（replay harness）。
- ISSUE-0011（hand/action schema freeze、F3b で決着予定）。
- GitHub issue #8（Phase F）/ Epic #4。

## Related Commits

- （本タスクの commit を後で追記）
