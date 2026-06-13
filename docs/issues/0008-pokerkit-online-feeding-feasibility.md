# Issue 0008: pokerkit を live エンジンとして incremental 駆動できるかの実現性

## Date

2026-06-03

## Status

<!-- One of: Open / Investigating / Fixed / WontFix / Duplicate -->
Fixed（2026-06-03 spike で feasibility 確認。pokerkit 0.7.4 で必要 API を実機検証。実装は R2）

## Update (2026-06-03, spike 実施)

`pokerkit==0.7.4`（`>=0.5.0` を満たす）で `NoLimitTexasHoldem.create_state(...)` を実機検証し、
`PokerEngine`（`docs/contracts/hand-reconstruction.md` §2）が必要とする API がすべて揃うことを確認した:

- **actor / 合法手 / 金額**: `state.actor_index`（手番, ポジション順）、`state.can_fold()` /
  `state.can_check_or_call()` / `state.checking_or_calling_amount`（= amount_to_call）、
  `state.can_complete_bet_or_raise_to(x)` / `state.min_completion_betting_or_raising_to_amount` /
  `state.max_completion_betting_or_raising_to_amount`（min/max raise）。
- **カード不要で betting 駆動可**: `Automation.HOLE_DEALING` でダミーカードを自動配布し、ホールカードを
  知らなくても betting state machine を回せる（live ではカードは RFID が source。pokerkit のダミーは
  状態機械駆動専用で hole-card 記録には使わない）。
- **不正額の拒否**: `complete_bet_or_raise_to(150)`（min 400 未満）→ `ValueError "The amount 150 is
  below the minimum allowed 400."`。`can_complete_bet_or_raise_to(150)` → False。これを「合法手への
  射影」と「pokerkit 拒否 = needs_review」に使える（ADR-0009）。
- **ストリート完了 / hand 終了**: 全員 call で `state.street_index` 自動進行・`actor_index` リセット。
  fold で 1 人になると `actor_index is None` / `status False` を検出可。
- **side-pot**: `state.pots`（各 `pot.amount` / `pot.player_indices`）。`CHIPS_PUSHING` / showdown
  automation 後は自動分配後の値になるため、**分配前にスナップショット**するか当該 automation を外す。

### R2 への設計含意

1. **announced winner の優先**: pokerkit の auto-showdown はダミーカードで誤った勝者を出す。pokerkit
   backend では showdown/push automation を外し、**アナウンス勝者へ手動 push**（または pot 額のみ採用）する。
2. **seat↔index 写像**を `new_hand()` で固定（`hand-reconstruction.md` §3）。
3. **`apply_action` の amount 意味**: 現 GameStateManager は「追加チップ額」、pokerkit は「to 総額」。
   変換（call=`checking_or_calling_amount`、bet/raise=to 総額へ snap）は R3 `apply_corrections` で行う。
   R2 の pokerkit backend は default-off（`engine.backend=legacy`）で導入し、live 既定動作は不変。

**結論**: feasibility 確認済み。ADR-0009 の gate を解除。R2 は default-off フラグで安全に着手できる。

## Severity / Priority

- Severity: High（ADR-0009 Accepted / R2 実装の gate。これが NG だと engine 設計を見直す）
- Priority: P1

## Area

reconstruct / core (game_state) / dependency (pokerkit)

## Expected Behavior

ADR-0009 のとおり `pokerkit.State`（`pokerkit>=0.5.0`）を live ルール権威にするには、各 action 時点で
**incremental に**以下を読み書きできる必要がある（`docs/contracts/hand-reconstruction.md` §2 の
`PokerEngine.legal_context()`）:

- 現在の **actor**（手番の player index）。
- **合法手集合**（fold / check / call / bet-or-raise-to のレンジ）。
- **amount_to_call** / **min_raise** / **max_raise**。
- NLHE state の構築（starting_stacks + blinds 自動 post）、`fold()` / `check_or_call()` /
  `complete_bet_or_raise_to(amount)`、ホール/ボードカードの deal、**main/side pot** の読み取り。

## Actual Behavior

- `pokerkit>=0.5.0` は `requirements.txt:4` に宣言されているが、**コードのどこからも import されていない**
  （`output/phh_exporter.py` は手書き TOML）。実際の online-feeding API の形・呼び出し順・例外挙動は
  **本リポジトリで未検証**。
- 本実行環境に pokerkit は **未インストール**のため、ここでは API を実測できない。

## Reproduction

設計前提の検証（バグではなく gate）:

1. `pip install pokerkit>=0.5.0` した環境で、NLHE の `State` を starting_stacks + blinds から構築する。
2. 1 アクションごとに actor / 合法手 / amount_to_call / min_raise を読めるか確認する。
3. 不正な額の `complete_bet_or_raise_to` が **例外**で弾かれるか（境界での推定がこの拒否を `needs_review` に
   使う, ADR-0009 §Decision-2）。
4. 3-way all-in（スタック差あり）で main/side pot が正しく算出されるか確認する。

## Root Cause

ADR-0009 は「既存の宣言済み依存 pokerkit を初めて live で使う」決定であり、その API 実現性が未確認のまま。
`game_state.py:31/91/162` の「Phase 3 で PokerKit に差し替える」TODO も同じ前提を未検証で持っていた。

## Fix

未対応（R1〜R2 着手時の spike で確定）。確定したら:

- pokerkit の online API を薄い wrapper（`PokerkitGameState`）に閉じ込め、`PokerEngine` Protocol へ適合させる。
- 露出が薄い項目（amount_to_call / committed 等）は engine 実装内の **shadow tracker** で補完する
  （interface は不変、`hand-reconstruction.md` §2 注記）。
- もし incremental 駆動が現実的でなければ、ADR-0009 を見直し（自前ベッティングエンジン案へ）—— その場合は
  新 ADR を起こす。

## Regression Test

- 確定後: `tests/test_reconstruction.py`（golden replay, ADR-0010）が pokerkit backend で緑。
- `tests/test_game_state.py` が legacy backend で緑のまま（regression 非発生）。

## Affected Files

- `core/game_state.py`（`PokerkitGameState` 実装先）
- `requirements.txt`（pokerkit, 宣言済・未 import）
- `docs/contracts/hand-reconstruction.md` §2（`PokerEngine` interface）

## Related Worklog

- `docs/worklog/2026-06-03-rules-aware-reconstruction-planning.md`

## Related ADRs

- `docs/adr/0009-pokerkit-live-rules-authority.md`（本 issue が gate）

## Related Commits

- 本 issue と同じコミット（reconstruction engine design planning）

## Notes

ADR-0009 を Accepted にする前に解消すべき唯一の hard blocker。spike は実装ではなく feasibility 確認に留める。
