# Issue 0033: RFID の「カード検出」を actor 証拠に使っており、配っただけで誤 fold が起きる

## Date

2026-09-12

## Status

Fixed（2026-09-12, P0a）

## Severity / Priority

- Severity: High（**オーナーが唯一リアルタイム精度を要求した fold** を壊す）
- Priority: P0（収集中のデータを壊し続けている）

## Area

integration（`integration/engine.py:_resolve_actor` / `_pop_nearest_rfid_seat`）

## Expected Behavior

RFID の seat 読みは「その席にカードが**存在する**」という観測であって、「その席が**行動した**」
という観測ではない。アクター推定の証拠にするなら、配布・置き直しと行動を区別できなければならない。

## Actual Behavior

`_resolve_actor` は `_pop_nearest_rfid_seat(event.timestamp)`（**席を問わず** ±2 秒以内で最も近い
seat 検出イベント）を取り、その席を `event.seat`（明示発話席）**より優先**して actor とする。
prior と異なれば `fold_through` で間の席を silent fold する。

ホールカードの配布は数秒で最大 16 件の検出イベントを生む。プレイヤーが持ち上げたカードを
置き直しても 1 件出る。**どちらも「行動した」と解釈される。**

golden fixture `tests/fixtures/reconstruction/out-of-turn-rfid` が、この挙動を**正解として固定**している:

```
t=4000.0 audio  new_hand
t=4000.8 rfid   seat 1 に Ah を検出      ← 配布と区別できない
t=4001.0 audio  「コール」（席の言及なし）
→ 期待値: seat 3 を fold 合成（conf 0.3）+ seat 1 に call 200（conf 0.888, source.rfid=true）
```

## Root Cause

ADR-0009 §4 /（ISSUE-0009 の暫定方針）で「物理証拠 > 明示発話 > prior」という優先順位を採ったこと。
「物理証拠」として使えるのは *chip motion*（camera）のような**行動の観測**であって、カードの
存在検出ではない。カメラを廃止（仕様 v3.0）した結果、行動を観測するセンサーが無くなったのに、
存在検出がその席に繰り上がった。

`_pop_nearest_rfid_seat` の ±2 秒窓は、**decode 完了時刻でスタンプされた音声イベント**に対して
測られており（`audio/recognizer.py:parse_action` が `time.time()`）、発話時刻とも揃っていない。

## Fix

ADR-0056 の **P0a**（実装済）:

- `_resolve_actor` から **RFID appear を actor 証拠として外した**。sensed は
  **明示発話席（`event.seat`）のみ**。戻り値も `(actor, conflict, folded)` に縮めた
  （RFID イベントを返す必要が無くなった）。
- `_pop_nearest_rfid_seat`（席を問わず最近傍を取り出す）を**削除**。
- RFID の corroboration は `_pop_matching_rfid_event`（**同席限定**）に切替。こちらは
  「別席へ actor を移す」力を持たないので無害で、confidence の裏付けとしては従来どおり効く。
- golden `out-of-turn-rfid` を **`rfid-appear-is-not-an-action` へ改名して期待値を作り直した**
  （旧期待値は不具合を正解として固定していたため、そのままでは残せない）。新しい期待値は
  「seat 1 のカードが検出されても actor は prior(seat 3) のまま・fold 合成なし」。
- 恒久的には ADR-0056 の事後推定で、プレゼンス**遷移**を非対称な尤度として扱う（D4）。

## Regression Test

- golden `tests/fixtures/reconstruction/rfid-appear-is-not-an-action`（配布 → 席の言及が無い
  「コール」→ actor は prior のまま）。
- `tests/test_phase_d2_wiring.py::TestRfidIsNotActorEvidence` 3 件:
  他席への配布で actor が動かない / 同席の読みは裏付けとして残る / 明示発話席は従来どおり勝つ。

## Affected Files

- `integration/engine.py`
- `tests/fixtures/reconstruction/rfid-appear-is-not-an-action/`（`out-of-turn-rfid` から改名・作り直し）
- `tests/test_phase_d2_wiring.py` / `tests/test_reconstruction.py` / `tests/test_confidence_calibration.py`（コメント）

## Related

- ADR-0056（P0a, D4）/ ISSUE-0031 / ISSUE-0009（優先順位の出所）/ ADR-0009 §4
- ADR-0055（マック観測。fold の**時刻**には使うが**判定**には使わない、という区別の延長線）
