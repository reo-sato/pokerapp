# Issue 0002: Manual action actor mismatch の permissive warning が live state を汚す

- **Status**: Resolved (2026-05-22)
- **Resolved by**: Phase 5-J / [ADR-0002](../adr/0002-manual-action-actor-mismatch-strict-reject.md)
- **Reported**: 2026-05-22 (Phase 5-I 完了直後の residual scope レビュー)
- **Related**: [Issue 0001](0001-manual-input-poker-rule-violations.md) (Phase 5-I)

## Symptoms

Phase 5-I で manual input を IntegrationThread 経由 (`_handle_manual_action_event`)
に統一したとき、actor mismatch (= `event.seat != bs.actor_seat`) は **permissive
review** として扱った: `gs.apply_action` / `bs.update_after_action` は呼び、
`ActionRecord.needs_review=True` を立てて積むだけ。これは Phase 5-I の
[ADR-0001](../adr/0001-route-manual-input-via-integration-thread.md) で意図した
仕様だが、運用してみると以下の問題が観測された:

### 1. `BettingState.actor_seat` が "次の actor" に進んでしまう

seat 3 が現 actor なのに seat 1 が誤って raise を入れた場合、apply の副作用で
`bs.update_after_action(1, "raise", ...)` が走り、`actor_seat` は seat 1 の
左隣 (= seat 2) に進む。これにより本来の actor (seat 3) は permanent に
skip された状態になり、後続の **正規入力もこの汚染された state を前提に
動く**。

### 2. `current_bet` / `last_aggressor` も汚染される

raise が actor mismatch 経由で apply されると `bs.current_bet` も入力 amount に
更新され、`last_aggressor = event.seat` になる。reconstruct (Phase 3 以降)
で beam を回せば偽 raise を弾けるが、live UI を見ている operator は
「汚染された current_bet」を信じて次の入力を組み立てる。

### 3. `needs_review=True` flag がノイズに埋もれる

`needs_review` は他にも複数経路で立つ:
- audio の amount mismatch (`infer_action` 内)
- call < to_call
- bet vs raise 混同
- reconstruct diff (`HandReconstructor._compute_diff`)
- patch_proposal の存在

actor mismatch だけを後から identify するには `ActionRecord.source` や log を
照合する必要がある (= operator の review コストが上がる)。

### 4. 「audio 補正用途」は別経路で支えるべきだった

ADR-0001 では「audio が滑った seat の代わりに別 seat の action を補正する」
ことを想定して permissive を選んだが、その用途は Phase 5-G/H で導入した
`patch_proposal` / `apply_patch_proposal` で扱うのがクリーンな設計。
live state への書き込みパスでやるべきではない。

## Root cause

Phase 5-I の ADR-0001 で permissive を採用した時、上記の state 汚染への
影響を過小評価していた。具体的には:

- ADR-0001 の Consequences "actor_seat mismatch is permissive" は
  `needs_review=True` を立てるだけで「人手で見つかる」と仮定していたが、
  実際は ad-hoc 入力ミスのほうが多く、review queue を発散させる方向に作用する。
- Phase 5-G/H で patch_proposal apply 経路が整備されたことで、後付け補正の
  needs を live state ではなく advisory layer で吸収できる構造ができた。
  Phase 5-I の時点ではこの代替経路が無かったので permissive が妥当に見えたが、
  Phase 5-J 時点では構造が変わっている。

## Resolution

Phase 5-J ([ADR-0002](../adr/0002-manual-action-actor-mismatch-strict-reject.md))
で actor mismatch を **strict reject** に変更:

- `_handle_manual_action_event` で `event.seat != bs.actor_seat` を検出したら
  早期 return。`gs.apply_action` / `bs.update_after_action` / `_current_actions`
  / `on_action` は **一切触らない**。
- 代わりに `logger.warning` + 新 callback `on_manual_rejected(ManualActionRejection)`
  を発火。GUI は `_apply_manual_rejection` でレビュー log を出す。
- `ManualActionRejection` dataclass は `seat` / `attempted_action` /
  `attempted_amount` / `expected_actor` / `reason` / `timestamp` を持つ。

audio 経路の actor mismatch (permissive review) は **変更しない**: audio は
observation で reconstruct で修正できるが、manual は意図的入力で reject が
自然、という非対称を意図的に維持する。

### 「audio の seat を補正したい」要求はどう満たすか

Phase 5-G/H の patch_proposal apply 経路:
1. audio が間違った seat の action を積む。
2. operator は手動 patch 経路 (`apply_patch_proposal`) で `seat_payouts` /
   `resolution_type` 等を補正する (= advisory level の修正)。
3. live `BettingState` は不変だが、`_last_summary_by_hand_id[hand_id]` の
   in-memory copy は patched version になる。

これにより live state の不変条件を保ちつつ、advisory 補正の経路も残せる
(= Phase 5-I の妥協理由は Phase 5-J で別経路でカバーできる)。

## Verification

新規 / 更新テスト:

### tests/test_manual_action.py (更新 + 新規 7 件)

- `TestManualActionBasics::test_consecutive_same_seat_raises_strict_rejected`
  (更新: 旧 `..._flagged_as_review`): 2 回目 raise が apply されず callback で
  通知される
- `TestManualActionContractWithGui::test_full_scenario_from_user_log`
  (更新): user 報告シナリオの 2 回目 raise は strict reject、後続の
  seat 1 call は通る
- `TestManualActionStrictReject` (新規 7 件):
    - `test_reject_does_not_append_record`
    - `test_reject_does_not_mutate_betting_state`
    - `test_reject_does_not_mutate_game_state`
    - `test_reject_does_not_fire_on_action`
    - `test_reject_fires_on_manual_rejected_with_payload`
    - `test_actor_match_still_applies`
    - `test_uninitialized_betting_state_does_not_reject`

### tests/test_gui.py (新規 4 件)

- `test_on_manual_rejected_pushes_to_queue`
- `test_apply_manual_rejection_logs_review_message_with_seats`
- `test_apply_manual_rejection_unknown_reason_still_logs`
- `test_actor_match_manual_submit_does_not_log_reject`

合計 Phase 5-J 11 件追加。全 663 件 green (= 652 baseline + 11)。

## Lessons learned

- 「permissive + needs_review」は **多次元の review trigger が既にある** 状況
  では新規 trigger を埋もれさせる方向に作用する。strict reject + 専用通知
  (= 別 channel) のほうが operator UX として優れることが分かった。
- ADR-0001 を書いた時点では Phase 5-G/H の patch_proposal apply 経路が未整備
  だったので permissive 採用は理にかなっていた。Phase 構造が変わったら ADR
  を見直して supersede する (= ADR は immutable だが supersede 経路で更新可能)。
- live state への書き込みパスと advisory 補正パスを **意味的に分離する**:
  - live (= `BettingState` / `GameStateManager` / `on_action`): 正規 turn のみ
  - advisory (= `_last_summary_by_hand_id` / patch_proposal apply): operator の
    後付け補正
  この分離が今回の strict reject の根拠。
