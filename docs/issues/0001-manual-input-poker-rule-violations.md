# Issue 0001: Manual GUI input bypasses BettingState, 5 件のポーカールール違反

- **Status**: Resolved (2026-05-22)
- **Resolved by**: Commit `b7d52ab` (Phase 5-I) /
  [ADR-0001](../adr/0001-route-manual-input-via-integration-thread.md)
- **Reported**: 2026-05-22 (user session log, 13:59-14:10)

## Symptoms

ユーザーがセッション中に手動入力を多用したログから、以下 5 件の poker rule 違反
が観測された:

### 1. 同一 seat の連続 raise

```
13:59:23  [preflop] 席3 c raise 3 pot=3 ...
13:59:35  [preflop] 席3 c raise 5 pot=8 ...
```

席1 / 席2 が action していないのに、seat 3 が 2 回連続で raise している
(= turn order 違反)。

### 2. `call 0` がそのまま記録される

```
14:09:55  [preflop] 席1 c call 0  pot=8 ...
14:09:59  [preflop] 席2 c call 0  pot=8 ...
```

operator が "call" ボタンを押した時に amount entry が空欄だったため、
amount=0 のまま記録された。`current_bet=5` の状態なので、本来 to_call は 5
で auto-fill されるべき。

### 3. bet と raise の混同

```
14:10:12  [preflop] 席3 c bet 7 pot=8 ...
```

preflop で既に raise が立っている状態 (`is_opened=True`) で "bet" が記録された
(本来は raise が canonical)。

### 4. street が preflop のまま動かない

5 件の action 全てが `[preflop]` のまま。`BettingState.current_bet` /
`actor_seat` / `player_contrib_this_street` が更新されないため、street 進行
判定そのものが機能していなかった。

### 5. SB/BB auto-post が見えない

このセッションでは `SB_POST` / `BB_POST` の ActionRecord が見当たらない
(audio で `new_hand` を投入する経路でないと auto-post が走らない既存仕様。
この点は本 issue では仕様の継続として扱う、resolution 参照)。

## Root cause

`gui/dashboard.py:_cmd_manual_action` (旧実装) が
`GameStateManager.apply_action(seat, action, amount)` を直接呼んでいた。

`GameStateManager.apply_action` は以下しかしない:
- ``seat`` の stack を amount だけ減らす
- pot に amount を足す
- ``turn_idx`` を round-robin で進める

これは BettingState には **一切触れない**。BettingState は以下を保持する別の
canonical state:
- `actor_seat` (= 真の turn order, round-robin より厳格)
- `player_contrib_this_street` / `player_contrib_hand` (= to_call の計算源)
- `current_bet` / `is_opened` / `last_raise_to` (= bet vs raise の判別)

音声経路 (`_handle_audio_event`) は `gs.apply_action` の **後に**
`bs.update_after_action` を呼んで BettingState を同期する。加えて
`infer_action` 経由で:
- `call 0` → `bs.call_amount_for(seat)` で to_call 補完
- bet / raise の整合性チェック
- actor_seat 違反検出 → `needs_review`

を全て行う。GUI の手動経路がこの一連の defensive 処理を全くやっていなかったため
5 件全てが素通りしていた。

## Resolution

Phase 5-I (commit `b7d52ab`) で `ManualActionEvent` + `IntegrationThread.manual_queue`
+ `_handle_manual_action_event` を新設し、GUI からの手動入力を音声経路と同等
品質で処理する。

設計判断 (Option A vs B vs C の選択理由) は
[ADR-0001](../adr/0001-route-manual-input-via-integration-thread.md) 参照。

### 5 件の symptom に対する resolution

| # | Symptom | Resolution |
|---|---|---|
| 1 | 同一 seat 連続 raise | `_handle_manual_action_event` が `bs.actor_seat` と照合し mismatch なら `needs_review=True` (permissive、apply 自体は通す) |
| 2 | `call 0` のまま | `bs.call_amount_for(seat) > 0` なら amount を auto-fill |
| 3 | bet と raise 混同 | `bs.is_opened` 下で `bet` action なら `needs_review=True` |
| 4 | street stuck on preflop | `bs.update_after_action` が呼ばれるので `current_bet` / `actor_seat` / `player_contrib_this_street` が正しく追跡される。なお street 自動進行は board cards / `advance_street` audio event 依存 (= 別仕様、本 issue ではスコープ外) |
| 5 | SB/BB auto-post 不在 | 仕様継続: SB/BB auto-post は `bs.start_hand` のみ。`new_hand` audio event (または将来追加する manual `new_hand` event) を経由しない限り blinds は自動 post されない。manual hand を最初から走らせる場合の運用としては operator が `new_hand` を別途 trigger する必要がある (= 本 issue では追加対応せず) |

## Verification

- `tests/test_manual_action.py::TestManualActionContractWithGui::test_full_scenario_from_user_log`
  はユーザー報告ログをほぼそのまま再現し、2 回目の seat 3 raise で
  `needs_review=True` が立つこと、続く `call 0` が auto-fill されることを assert。
- `TestManualActionBasics::test_call_zero_auto_fills_to_call` で 2 の resolution
  を unit test。
- `TestManualActionBasics::test_call_zero_with_no_bet_becomes_check` で
  `to_call=0` のとき `call 0` が `check` に置き換えられることを確認。
- `TestManualActionBasics::test_consecutive_same_seat_raises_flagged_as_review`
  で 1 の resolution を unit test。
- `tests/test_gui.py` Phase 5-I 追加分 4 件で GUI が `gs.apply_action` を直叩き
  しないこと、`manual_queue.put` を経由することを確認。

Total: 11 件の新規 test、全 652 件 green。

## Lessons learned

- "GUI が直接 GameStateManager を触る" を許すと、business logic を持つ BettingState
  / EvidenceLog / Beam が単純にスキップされる。
- 経路を 1 つの IntegrationThread に絞ることで poker rule の defensive 処理が
  漏れなく走る。今後 RFID / camera / 外部 API 経由の action 入力経路を追加する
  場合も同じパターン (= 専用 event 型 + 専用 queue + IntegrationThread handler)
  を踏襲する。
