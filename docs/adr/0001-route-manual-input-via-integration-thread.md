# ADR-0001: Route GUI manual input through IntegrationThread (Phase 5-I)

- **Status**: Accepted
- **Date**: 2026-05-22
- **Commit**: `b7d52ab`
- **Phase**: 5-I

## Context

Phase 5-I 以前、GUI の「手動 action」経路 (`gui/dashboard.py:_cmd_manual_action`)
は `GameStateManager.apply_action(seat, action, amount)` を直叩きしていた。
これは stack / pot / round-robin turn_idx は更新するが、`BettingState` には
触れない。

`BettingState` は以下を保持する canonical な state:
- `actor_seat` (= turn order の真実)
- `player_contrib_this_street` / `player_contrib_hand` (= to_call 計算源)
- `current_bet` / `is_opened` / `last_raise_to` (= bet vs raise 判別)
- SB/BB auto-post via `start_hand`

音声経路 `_handle_audio_event` は `gs.apply_action` の後に
`bs.update_after_action` を呼び、加えて `infer_action` 経由で:
- amount_only の補完 (= `call 0` → to_call 値補完、bet/raise 判別)
- actor_seat の検証
- `needs_review` フラグ

を全て行う。GUI の手動経路だけが BettingState をスキップしていたため、
operator が手で押す action がポーカールール違反 (issue 0001 参照) を
通してしまっていた。

修正の目的は **GUI と audio が同じ BettingState + GameStateManager 不変条件を
共有する** こと。3 つの形が考えられた。

## Decision

新しいイベント型 `ManualActionEvent(seat, action, amount, timestamp)` を導入し
(`core/events.py`)、`IntegrationThread.manual_queue: queue.Queue` 経由で GUI →
IntegrationThread に流す。IntegrationThread の run loop は audio drain の **前** に
`_drain_manual_queue` を回し、各 event を `_handle_manual_action_event` で処理
する。この handler は `_handle_audio_event` と同等の defensive 処理:

- actor_seat を `bs.actor_seat` と照合し mismatch なら `needs_review=True`
  (permissive — apply は通す)
- `amount=0` の call を `bs.call_amount_for(seat)` で auto-fill
- `to_call=0` のときの `call 0` は check に置換
- bet / raise の整合性チェック (`bs.is_opened` 下で bet なら `needs_review`)
- `gs.apply_action` → `bs.update_after_action` の順で適用
- `ActionRecord` を組み立て `on_action` callback を発火

ただし **EvidenceLog / BeamEngine / `_track_evidence` には流さない** (operator
intervention は observation ではない、後述の Consequences 参照)。

GUI 側 (`gui/dashboard.py:_cmd_manual_action`) は `manual_queue.put(event)` だけを
行い、business logic は持たない。

### Alternatives considered

#### Option B: fix `_cmd_manual_action` in-place

GUI 自身に `BettingState.update_after_action` と actor_seat 検証を持たせる案。

**Rejected** because:
- `_handle_audio_event` との logic 重複が発生する。
- 非自明なポーカールール logic を Tk main thread で走らせることになる
  (`CLAUDE.md` の「GUI スレッドからビジネスロジックを呼ばない」規則に違反)。
- `BettingState` の lock-free 前提 (= 単一 thread からのみ書き換える) が崩れる
  リスクがある。

#### Option C: shared helper that both GUI and audio handler call

`gs.apply_action` + `bs.update_after_action` を共通 helper `apply_player_action()`
に括り出す案。

**Rejected** because:
- 音声経路 (`_handle_audio_event`) は EvidenceLog 書き込み、`beam.step_audio`、
  `_track_evidence`、`infer_action` distribution 評価 など追加の責務を持っており、
  手動経路は **これらを意図的に走らせたくない** (operator intervention は
  observation ではない)。
- 共通 helper を作ると、結局フラグだらけになるか副作用を手動経路に引っ張り込む
  ことになる。
- イベント型を分けて drain 経路を分ける Option A のほうが「manual と audio は
  別 source」という意味的区別が明示される。

#### Option A (chosen): new event type + dedicated queue + dedicated handler

採用理由:
- IntegrationThread が business logic の単一 owner、GUI は event 発生源だけ
  という既存設計と整合 (= audio thread と同じ shape)。
- manual と audio で意図的に違える処理 (= EvidenceLog / Beam に流すか否か)
  を分岐ではなく event 型レベルで分離できる。
- 既存音声経路を一切変更しない。

## Consequences

### Pros

- GUI と audio が同じ `BettingState` / `GameStateManager` 不変条件を共有する。
  issue 0001 の 5 件のポーカールール違反は本 ADR の実装で resolved。
- `_handle_manual_action_event` は IntegrationThread に住むので、Tk main thread
  は event push だけになり「GUI thread でビジネスロジック禁止」規則を守れる。
- 音声経路は無変更。Phase 5-A〜5-H の semantics は無傷。

### Cons / Scope-out

#### 1. Manual events は EvidenceLog / BeamEngine に乗らない

設計上の判断: manual events は **observation ではなく operator の介入**。
``HandReconstructor`` のオフライン再生 (= raw observation ベースの sequence MAP)
には混ぜない。これにより `HandReconstructor._compute_diff(online, offline)` は
「online には manual action があるが offline には無い」差分を出す。

これは **意図的**: operator が手で補正したことを reconstruct が検出して
review 表示に出すための signal。canonical な book of record (online JSON) には
全 manual action が含まれる。

#### 2. actor_seat mismatch は permissive (= warning のみ)

operator が「audio が滑った seat の代わりに別 seat の action を補正する」
ケースを想定して、strict reject ではなく `needs_review=True` を立てる
permissive 設計を採用。

strict mode (= mismatch なら apply 自体を拒否) は将来オプションで追加可能だが、
現状の運用では誤入力よりも audio mis-recognition の補正が dominant なので
permissive を default にする。

#### 3. Street advance のトリガーは変更しない

manual action からの「全員 call で round 終了」自動判定は本 ADR の対象外。
street 遷移は引き続き board cards (RFID / 手動入力) または `advance_street` audio
event に依存する。これは音声経路にも実装されていない既存仕様で、本 ADR は
"manual と audio を揃える" 範囲のみを対象とする。

## References

- Issue: `docs/issues/0001-manual-input-poker-rule-violations.md`
- Worklog: `docs/worklog/2026-05-22-phase-5-I.md`
- Commit: `b7d52ab feat(Phase 5-I): route GUI manual input through IntegrationThread`
- 該当コード:
    - `core/events.py:ManualActionEvent`
    - `core/event_queue.py:make_manual_queue`
    - `integration/engine.py:_drain_manual_queue` / `_handle_manual_action_event`
    - `gui/dashboard.py:_cmd_manual_action`
- 関連テスト:
    - `tests/test_manual_action.py` (7 件)
    - `tests/test_gui.py` (Phase 5-I 追加 4 件)
