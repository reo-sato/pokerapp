# ADR-0002: Manual action actor mismatch is strict reject (Phase 5-J)

- **Status**: Accepted
- **Date**: 2026-05-22
- **Phase**: 5-J
- **Supersedes**: 部分的に [ADR-0001](0001-route-manual-input-via-integration-thread.md)
  の "actor_seat mismatch is permissive (= warning only)" の Consequences 項目
  (= 設計判断としては Phase 5-I の妥協点だった部分を Phase 5-J で厳格化)

## Context

Phase 5-I ([ADR-0001](0001-route-manual-input-via-integration-thread.md)) で
manual input を IntegrationThread 経由にしたとき、`_handle_manual_action_event`
は actor mismatch を検出しても apply を通し `needs_review=True` だけ立てる
**permissive** 設計を採用した。理由として ADR-0001 は次を挙げていた:

> operator が「audio が滑った seat の代わりに別 seat の action を補正する」
> ケースを想定して、strict reject ではなく permissive (= `needs_review` で
> 印を付ける) にしている。

しかし運用してみると以下の問題が浮かんだ (詳細:
[issue 0002](../issues/0002-manual-action-permissive-warning-too-soft.md)):

1. **review がノイズに埋もれる**: `needs_review=True` flag は他にも複数の
   経路 (audio の amount mismatch、bet/raise 混同、call < to_call、reconstruct
   diff など) で立つので、actor mismatch は埋もれて operator が後から判別
   しにくい。
2. **apply してしまうので state が壊れる**: actor mismatch を apply すると
   `BettingState.actor_seat` / `current_bet` / `contrib` が「正しくない順番」で
   進む。後続の正規入力もこの汚染された state を前提に動くので、結局
   reconstruct までいかないと正しい列が出ない。
3. **audio 補正用途は別経路で支えるべき**: 「audio が違う seat を取った」場合
   の補正は `patch_proposal` / `apply_patch_proposal` (Phase 5-G/H) で行うのが
   設計上クリーンで、live state への書き込みパスでやるべきではない。

issue 0002 は production の運用問題ではなく、live state の不変条件
(invariant) を強くする方向の設計改善として扱う。

## Decision

manual action の actor 検証を **strict reject** に変更する。`event.seat !=
bs.actor_seat` を検出したとき:

- `gs.apply_action()` を呼ばない
- `bs.update_after_action()` を呼ばない
- `_current_actions` に record を積まない
- `on_action` callback を呼ばない
- `logger.warning(...)` を出す
- 新コールバック `on_manual_rejected(ManualActionRejection)` を発火 (GUI が
  review log に表示する)

`ManualActionRejection` dataclass を新設し、operator が必要な情報を全部持つ:
- `seat` (= 入力された seat)
- `attempted_action` / `attempted_amount` (= 入力された action / amount)
- `expected_actor` (= mismatch 時の `bs.actor_seat`)
- `reason` (= 短い English 識別子。Phase 5-J では `"actor_mismatch"` のみ)
- `timestamp`

reject 時に **dummy record を `needs_review=True` で積む** 方式は採用しない。
理由は §"Alternatives considered" を参照。

### audio 経路は permissive のまま継続

audio 経路の actor mismatch (= `infer_action` 等で `needs_review=True` を立てる)
は Phase 5-J で **変更しない**:

- audio は observation で、後段 reconstruct (HandReconstructor) が修正
  candidate を出す前提の設計。
- manual は operator の意図的入力で、observation ではないので reject が自然。

(= manual と audio で actor mismatch の取り扱いを意図的に非対称にする。)

### Alternatives considered

#### Option B: dummy record with `needs_review=True`

reject 時に `ActionRecord(seat=X, action=Y, amount=Z, needs_review=True,
note="actor_mismatch")` を積む案。

**Rejected** because:
- 「観測された曖昧性」ではなく「operator が間違った seat を選んだ」事象を
  ActionRecord 列に混ぜる意味的な不一致が起きる。
- ``HandSummary.actions`` を読む下流コード (PHH exporter / json_writer /
  reconstructor) が「apply されていない record」を扱う特殊ケースを増やす
  ことになる。
- review queue が actor mismatch でも膨らみ、permissive の問題を再現する。

#### Option C: actor mismatch を config flag で permissive / strict 切替可能に

config (`config.json`) や constructor 引数で挙動を選べるようにする案。

**Rejected** because:
- 仕様ではなく挙動の choice を実行時に切替えると、test / reproduction の認知
  負荷が増える。
- Phase 5-G/H で patch_proposal apply 経路を整備済みなので、operator が
  「audio が滑った seat の action を補正したい」要求はそちらで満たせる。
- 後から strict のみに固定するときに deprecation 経路が必要になる。

#### Option D: actor mismatch を Exception で raise

`_handle_manual_action_event` で actor mismatch のとき例外を投げる案。

**Rejected** because:
- IntegrationThread の `_drain_manual_queue` で `try / except Exception` に
  食われてしまい結局 silent drop に近い。
- Exception はバグや I/O 障害を表現する手段で、operator の入力ミスを表現
  する手段としては重すぎる。
- callback + logger の組合せで GUI / ログの両方に出せる。

## Consequences

### Pros

- live `BettingState` / `GameStateManager` の不変条件を維持できる
  (= 正規 turn の seat だけが state を進める)。
- audio mis-recognition の補正は patch_proposal 経路 (Phase 5-G/H) に集約
  され、live と review の責務分離が明確になる。
- operator は actor mismatch のとき即座に review log で気づき、「正しい seat
  を選び直して再入力する」だけで recover できる (= apply 済 state を巻き戻す
  必要がない)。
- audio 経路は無変更なので Phase 5-I 以前のテストはそのまま green。

### Cons / Scope-out

#### 1. operator が「audio の補正」を live state で行いたい場合の経路は別に必要

actor が seat X だが、audio が間違って seat Y を取って fold を記録した、と
いう状況で operator が「いや本当は seat X が fold した」と直したい場合、
manual 経路では reject される (= seat X が現 actor)。これは設計どおりで、
代わりに patch_proposal (Phase 5-G/H) で audio が積んだ record を後から
修正する。Phase 5-J では new_hand 跨ぎの "audio undo" は対象外。

#### 2. reject 時の `_current_actions` に痕跡が残らない

reject されたイベントは EvidenceLog にも `_current_actions` にも入らないので、
後から「いつ何回 actor mismatch を operator が試みたか」を audit したい場合は
log file (logger.warning) からしか復元できない。これは optional な operator
action log (= 構造化 audit trail) の議論につながるが、Phase 5-J では既存
方針 (= manual events を observation に流さない) を維持する。

#### 3. `bs.is_initialized=False` のときの扱いは既存仕様

`BettingState` 未初期化 (= `new_hand` 前) のときは actor が定義できないので、
Phase 5-J でも actor 判定はスキップして apply する (Phase 5-I の挙動継続)。
manual-only でハンドを開始するシナリオは scope-out として残し続ける。

#### 4. config / runtime switch は導入しない

Option C で議論したように、permissive モードへの戻り口は作らない。将来必要に
なれば新 ADR で復活させる (= immutable ADR の supersede 経路に従う)。

## References

- Issue: [0002-manual-action-permissive-warning-too-soft](../issues/0002-manual-action-permissive-warning-too-soft.md)
- Worklog: [2026-05-22-phase-5-J](../worklog/2026-05-22-phase-5-J.md)
- Supersedes 部分: [ADR-0001](0001-route-manual-input-via-integration-thread.md)
  の "actor_seat mismatch is permissive" Consequences 項目
- 該当コード:
    - `core/events.py:ManualActionRejection`
    - `integration/engine.py:_handle_manual_action_event` (actor 検証ブロック)
    - `gui/dashboard.py:on_manual_rejected` / `_apply_manual_rejection`
    - `main.py` の IntegrationThread コンストラクタ呼び出し
      (`on_manual_rejected=dash.on_manual_rejected`)
