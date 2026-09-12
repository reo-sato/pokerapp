# Issue 0033: RFID の「カード検出」を actor 証拠に使っており、配っただけで誤 fold が起きる

## Date

2026-09-12

## Status

Open

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

**未着手**。ADR-0045 の **P0a**:

- `_resolve_actor` から **RFID appear を actor 証拠として外す**（明示発話席は残す）。
- golden `out-of-turn-rfid` は「配布と区別できない」ため**期待値を作り直す**（現在の期待値は
  不具合を固定しているので、そのまま残せない）。
- RFID の corroboration（`_pop_matching_rfid_event` による**同席**の裏付け）は従来どおり残す
  — こちらは「別席へ actor を移す」力を持たないので無害。
- 恒久的には ADR-0045 の事後推定で、プレゼンス**遷移**を非対称な尤度として扱う（D4）。

## Regression Test

未追加。修正時に「新ハンド直後に席のカードが検出されても、席の言及が無い発話で actor が
移らない」を固定する。

## Affected Files

- `integration/engine.py`
- `tests/fixtures/reconstruction/out-of-turn-rfid/`（作り直し）

## Related

- ADR-0045（P0a, D4）/ ISSUE-0031 / ISSUE-0009（優先順位の出所）/ ADR-0009 §4
- ADR-0044（マック観測。fold の**時刻**には使うが**判定**には使わない、という区別の延長線）
