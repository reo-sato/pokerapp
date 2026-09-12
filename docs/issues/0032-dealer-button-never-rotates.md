# Issue 0032: ディーラーボタンが回らず、ターン順 prior が (n-1)/n のハンドで誤っている

## Date

2026-09-12

## Status

Open

## Severity / Priority

- Severity: High（アクター推定の**最重要の証拠**が 2 人超の卓でほぼ常に誤る）
- Priority: P0（収集中のデータを壊し続けている）

## Area

core / poker_engine（`PokerkitGameState.new_hand`）

## Expected Behavior

仕様 `sprc_v4.docx` FR-05b: **ハンドごとにディーラーボタンが回転**し、`button_seat` /
`position_map` / `turn_order` が記録される。FR-26 はターン順（`current_turn_seat`）を
アクター推定の「**最優先**」証拠（確率 0.95 以上）と規定している。

## Actual Behavior

検証（2026-09-12, 6-handed で 3 ハンド連続）:

```
hand 1: 最初の actor = seat 3 / ブラインドを出した席 = [1, 2]
hand 2: 最初の actor = seat 3 / ブラインドを出した席 = [1, 2]
hand 3: 最初の actor = seat 3 / ブラインドを出した席 = [1, 2]
```

ボタンが回らないため、**毎ハンド同じ席が SB/BB を払い、同じ席が最初に行動する**。
`button_seat` はどこにも記録されていない。

## Root Cause

`core/poker_engine.py:new_hand` は `sorted(seats)` の並びで毎回 `create_state(...)` を呼ぶため、
pokerkit から見た席順が固定される。ボタン位置という概念を持っていない。CLAUDE.md も
「ディーラーボタン自動回転 / SB・BB 自動 post ❌ 未実装」と記載しており、**既知の未実装が
アクター推定の前提を崩していること**が見落とされていた。ADR-0009 §5 は「BTN 基準の安定全単射」を
予定していたが実施されていない。

## 影響

- ターン順 prior が (n-1)/n のハンドで誤る → `_resolve_actor` の prior も、記録される `street` /
  actor も系統的にずれる。
- ポジション名（UTG / BTN / CO …）の解釈が原理的にできない（`position_map` が無い）。
  仕様 §7 が推奨するディーラー発話「BTN、コール」を活かせない。
- 事後推定（ADR-0045）でも、ボタンが分からないハンドは prior を持てない。

## Fix

**未着手**。ADR-0045 の **P0b**:

- セッション設定 / CLI / GUI で `button_seat` を指定し、ハンドごとに回転させる。
- BTN 基準の seat↔index 全単射で pokerkit に渡す。
- `HandSummary.button_seat` / `position_map`、`ActionRecord.position` を additive 記録（仕様 §6.1/6.2）。
- `audio/recognizer.py` にポジション名の解釈を追加（FR-26 の 4 行目）。

## Regression Test

未追加。修正時に「n ハンド回してボタン・ブラインド・初手 actor が 1 席ずつ進む」を固定する。
既存 golden はアクター順が変わるため**作り直し**が必要。

## Affected Files

- `core/poker_engine.py` / `core/game_state.py` / `core/hand_log.py` / `audio/recognizer.py`
- `tests/fixtures/reconstruction/*`（期待値の作り直し）

## Related

- ADR-0045（P0b）/ ISSUE-0031 / ADR-0009 §5（未実施の約束）
- 仕様 `sprc_v4.docx` FR-05b〜FR-05h / FR-26 / §6.1 / §6.2 / §7
