# Issue 0028: 進行中のハンドが無いときのアクションが traceback になる（pokerkit backend）

## Date

2026-09-12

## Status

Fixed

## Severity / Priority

- Severity: Medium（イベントは黙って捨てられ、スレッドは生き残るが操作者には理由が分からない）
- Priority: P1（実プレイ通しテストの入口で必ず踏む。`n` を押す前のアクションは日常的に起きる）

## Area

integration（`integration/engine.py`）/ core（`core/poker_engine.py`）

## Expected Behavior

- 新ハンド前（`n` 未実行）・ハンド終了後にアクションや winner 宣言が届いても、**クラッシュせず**
  「次に何をすればいいか」が分かるログを出して当該イベントを落とす
  （CLAUDE.md エラーハンドリング方針「認識エラーでクラッシュしない」）。
- 確定済みハンドへの winner 再宣言でポットが二重に加算されない。

## Actual Behavior

実機 2026-09-12（`--cli`, backend=pokerkit, `audio.enabled=false`）:

```
ちぇっく
  → check
2026-09-12 14:00:45 [IntegrationThread] ERROR integration.engine: Error handling audio event: AudioEvent(action='check', …)
Traceback (most recent call last):
  File "…\integration\engine.py", line 467, in _handle_audio_event
    self._handle_legacy_action(event)
  File "…\integration\engine.py", line 507, in _handle_legacy_action
    seat = gs.get_current_player()
  File "…\core\poker_engine.py", line 215, in get_current_player
    raise RuntimeError("No actor (no active hand or hand over)")
RuntimeError: No actor (no active hand or hand over)
```

`IntegrationThread.run` の外側 try/except が握るのでスレッドは死なないが、操作者には
「`n` を押していない」ことが伝わらない。

## Root Cause

2 つの前提が噛み合っていなかった。

1. **dispatch の分岐が「空 legal_context = legacy backend」だと仮定していた**。実際には
   pokerkit backend でも **手番が無ければ** `legal_context()` は空を返す（新ハンド前 /
   ハンド終了後 / 全員オールイン後）。そのため rules-aware backend のイベントが
   `_handle_legacy_action` に落ち、そこの `gs.get_current_player()` が `RuntimeError` を投げた。
2. **`PokerkitGameState` の actor 系照会が `_hand_active` を見ていなかった**。`end_hand` 後も
   `pokerkit.State.actor_index` は値を持ったままなので、`legal_context()` は「終わったハンドに
   合法手がある」と答え、rules-aware 経路が `apply_action` → `ValueError("No active hand")` で
   別の traceback を出していた（`apply_action` だけが `_hand_active` を見ていた）。

同じ原因で `winner` も壊れていた: ハンドが無ければ `end_hand` が `RuntimeError`、確定済み
ハンドへの再宣言は `pot_total` を再計算して **勝者に二重加算**していた（黙ったデータ破損）。

## Fix

**core（境界を正す）**:

- `PokerEngine` protocol に `is_hand_active()` を additive 追加。
  - `PokerkitGameState`: `_state is not None and _hand_active`。
  - `GameStateManager`(legacy): 常に `True`（ハンドのライフサイクルを持たない = 挙動不変）。
- `PokerkitGameState` の `get_current_player` / `legal_context` / `is_legal_actor` が
  `_hand_active` を見るようにした（`apply_action` と同じ前提に揃える）。

**integration（落とし方を正す）**:

- `_current_actor_or_none()`: `get_current_player()` の `RuntimeError` を握って `None` を返す。
- `_handle_legacy_action`: actor が無ければ `_warn_no_actor` で案内して return。
  「ハンド進行中なのに手番が無い（= 記録が欠ける）」ときだけ `needs_review` を立てる。
  ハンドが無いときは付け先が無いので立てない。
- `winner` を `_handle_winner` に切り出し、`is_hand_active()` が偽なら確定しない
  （= 二重加算しない）。席を特定できないときも traceback ではなく案内ログ。

`is_hand_active()` が legacy で常に `True` なので、**legacy backend の挙動は不変**。

## Regression Test

`tests/test_no_active_hand_guard.py`:

- `TestActionWithoutActiveHand` — 新ハンド前 / ハンド終了後のアクションが例外にならず記録も
  作らない / WARN に「新ハンド」と action 名が出る / 落とした後に `n` すれば通常進行する /
  legacy backend は従来どおり記録する。
- `TestWinnerWithoutActiveHand` — ハンド前の winner はハンドを書き出さない / 再宣言で
  スタックが変わらない / 手番がある間の「席なし winner」フォールバックは不変。
- `TestIsHandActive` — pokerkit はライフサイクルを追う / legacy は常に `True`。

## Affected Files

- `integration/engine.py`
- `core/poker_engine.py`
- `core/game_state.py`
- `tests/test_no_active_hand_guard.py`

## Related

- ADR-0009（rules-aware backend 境界。空 `legal_context` を「legacy の印」としていた前提の補正）
- ISSUE-0026 / ADR-0053（同じ実機通しテストで見つかった board 位置の同期点）
- `docs/worklog/2026-09-12-no-active-hand-guard.md`

## 追記（2026-09-22, verify-v1 マージ）

verify-v1 の ADR-0047 B2/B5（「イベントの無音消失の全廃」）と合流し、「落として案内する」は
**unresolved レコード**（`actor_source="unresolved"`, `apply_ok=false`, `reason="no_active_hand"`,
needs_review）を `on_action` にだけ流す形になった。ゲーム状態と `HandSummary.actions` に入らない点は
本 issue の修正と同じで、黙って消えない分だけ監査しやすい。`_current_actor_or_none` /
`_warn_no_actor` は `_emit_unresolved` に吸収（案内ログは維持）。確定済みハンドへの winner 再宣言は
`is_hand_active()` で引き続き止める（B5 の「end_hand が失敗しても review 付きで書き出す」経路に
落とすと空 summary を量産するため）。席の無い winner は B5 の fallback 連鎖で補う。
CLI は unresolved を `[未適用] <action> (<reason>)` と表示する。
