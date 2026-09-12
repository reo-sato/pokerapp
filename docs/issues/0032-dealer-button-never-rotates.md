# Issue 0032: ディーラーボタンが回らず、ターン順 prior が (n-1)/n のハンドで誤っている

## Date

2026-09-12

## Status

Fixed (2026-09-12)

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

**実装済（2026-09-12）**。ADR-0045 の **P0b**:

- `core/positions.py`（新規・純粋ロジック）: `seat_order_from_button` / `next_button` /
  `position_names` / `position_map` / `seat_for_position` / `parse_position`。
- `PokerkitGameState` が **ハンドごとにボタンを 1 つ進め**（`new_hand`）、ボタンの次の席を先頭に
  した並びで `create_state` を呼ぶ（pokerkit はブラインドを index 0/1 に置き、末尾がボタン）。
  `button_seat` プロパティと `position_map()` を `PokerEngine` 境界に additive 追加。
- **初期値 `None` は最大の席番号**から始める（`next_button`）。こうすると 1 ハンド目の並びが
  `sorted(seats)` と一致し、ボタン導入前の挙動 = 既存 golden fixtures の 1 ハンド目が不変になる。
  回転はハンド 2 以降に現れる。
- `HandSummary.button_seat` / `position_map` と `ActionRecord.position` を additive 記録
  （仕様 §6.1 / §6.2）。schema は `hand` `1.1`→`1.2` / `action` `1.1`→`1.2`（optional 追加）。
- `audio/recognizer.py` がポジション名の言及を拾い（`parse_position`）、`AudioEvent.position` で
  持ち回る。席への解決は**ボタンを知っている engine 側**（`IntegrationThread._sensed_seat`）が
  行う。席番号（"シート3"）があればそちらが優先。卓に無いポジション名は無視する（手番は動かない）。
- `main.py --cli` / GUI のセッション設定で **1 ハンド目のボタン席**を訊く（空 Enter = 最大席）。
- `tools/table_monitor.py` にボタンとポジション名を表示（実機で回転を目視確認するため）。

**legacy backend はボタンを持たない**（`button_seat` は None・`position_map()` は空）。単純
ラウンドロビンを変えると rollback path の挙動不変契約が崩れるため、回転は rules-aware backend
だけに入れた。

## Regression Test

- `tests/test_positions.py`（54 件）: 並び / 回転（1 周でボタンが全席を訪れる）/ ポジション名の
  はしご / **ポジション名が実際にブラインドを出す席と一致すること**（heads-up は
  `["BB","BTN"]` = ボタンが SB。pokerkit の実挙動に合わせて実測で確定）/ 別名パースの偽陽性
  （"scoop" の "co" 等）/ legacy が回らないこと / 記録フィールドの直列化。
- `tests/test_position_evidence.py`: 「BTN、コール」が席番号と同じ明示証拠として actor を決める /
  席番号が優先される / 卓に無いポジションは無害に無視される / ハンドが変われば同じ "BTN" が
  別の席を指す / legacy では効かない。
- `tests/test_event_recorder.py`: `position` の envelope 往復（記録しないと replay が別 actor を
  選びうるため）。
- golden fixtures は 5 件とも **追加キーのみ**の差分で再生成（`button_seat` / `position_map` /
  `position`。既存の値は 1 つも変わらない = 1 ハンド目の挙動不変が確認できている）。
- `tests/test_poker_engine.py::test_rebuy_between_hands` と
  `tests/test_phase_d0_engine.py::TestFoldThrough::test_synthesizes_intermediate_folds` は
  「ボタン固定」を前提にしていたので、回転を前提にした形へ書き換えた（前者は次ハンドで
  ブラインドを出しうるので `stack + committed` を見る、後者は当該ハンドの state から手番順を取る）。

全体: 976 passed（skip 0, vision 除外）。

## Affected Files

- `core/positions.py`（新規）/ `core/poker_engine.py` / `core/game_state.py` / `core/hand_log.py`
- `core/events.py` / `audio/recognizer.py` / `integration/engine.py` / `integration/replay.py`
- `output/event_recorder.py` / `core/table_state.py` / `tools/table_monitor.py` / `main.py`
- `docs/contracts/schemas/{hand,action,reconstruction_event}.schema.json`
- `tests/fixtures/reconstruction/*`（期待値を additive に再生成）

## Related

- ADR-0045（P0b）/ ISSUE-0031 / ADR-0009 §5（未実施の約束）
- 仕様 `sprc_v4.docx` FR-05b〜FR-05h / FR-26 / §6.1 / §6.2 / §7
