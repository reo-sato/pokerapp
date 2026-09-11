# Issue 0026: ボードを下げて置き直すと RFID の位置と engine の board がずれ、同じ札が 2 か所に出る

## Date

2026-09-11

## Status

Fixed（新ハンドを唯一の同期点にした。実機での再確認は次の通しで）

## Severity / Priority

- Severity: High（ボードに同じ札が並び、枚数が水増しされてストリートが誤って進む）
- Priority: P1（ISSUE-0025 修正後の実機通しで発覚）

## Area

rfid / integration（`rfid/reader_thread.py`, `integration/engine.py`, `main.py`）

## Expected Behavior

ボードの内容は **RFID 側の位置割り当てと engine 側の `_board_positions` で常に一致**する。
1 組のデッキなので、ボードに同じカードが 2 枚並ぶことはない。

## Actual Behavior

実機 2026-09-11（`da17e7a`, ISSUE-0025 修正後）:

```
18:22:29,875 Board card [pos=1]: Kc — board ['Kc']
18:22:31,687 Board card [pos=2]: Qc — board ['Kc','Qc']
18:22:31,688 Board card [pos=3]: Jc — board ['Kc','Qc','Jc']   → flop（ここまで正しい）
18:22:32,705 Board card [pos=2]: Qc — board ['Kc','Qc','Jc']   （同じ位置に再発火 = ISSUE-0025 修正が効いている）
18:22:58,779 Board card [pos=4]: Jc — board ['Kc','Qc','Jc','Jc']   ← Jc が 2 か所
18:23:01,946 Board card [pos=3]: Qc — board ['Kc','Qc','Qc','Jc']   ← Qc が 2 か所
18:23:04,527 Board card [pos=2]: 7c — board ['Kc','7c','Qc','Jc']
```

ISSUE-0025 の「同じ UID の再発火が同じ位置になる」は直っている（`[pos=2]: Qc` が 4 回、
すべて 2 番）。残ったのは **異なる位置に同じカード名が並ぶ**こと。

## Root Cause

**engine の board は縮まないのに、RFID 側の位置空間はボードが空になるとリセットされていた。**

- engine（`_handle_board_rfid`）は `_board_positions[index] = card` で埋めるだけ。
  カードが外れても **イベントは出ない**ので board は縮まない（設計どおり = ハンド確定まで
  ボードを保持する）。
- RFID 側（`_sync_board_presence`）は「board reader の UID 和集合が空 → 位置記憶をクリア」と
  していた（ADR-0042 の「ハンドの切れ目」判定）。

よって **ボードを一度全部下げて、別の順で置き直す**と:

1. 下げた時点で RFID の位置は全部解放 + 記憶クリア。engine の board は `['Kc','Qc','Jc']` のまま。
2. 置き直すと RFID は 1 番から振り直す。順が違えば `Qc → 1` などになる。
3. engine は `_board_positions[1] = Qc` で上書きするが 2,3 には古い札が残る
   → `['Qc','Qc','Jc']` のように **同じ札が並ぶ**。位置 4 まで埋まれば turn も誤発火する。

実機のログはまさにこの並びで、ユーザーが検証中にボードのカードを入れ替えていた状況と一致する。
**同じ危険は「5 枚が同時に一瞬読めなかった」場合にも起こる**（firmware 側 hold=3 で確率は低いが 0 ではない）。

## Fix

**位置のリセットは「新ハンド」だけを同期点にする**（engine と RFID が必ず同じ状態から始まる）。

1. `rfid/reader_thread.py`
   - `_sync_board_presence`: **位置記憶（`_board_index_memory`）を捨てない**。いま盤上にある
     UID（`_board_index_by_uid`）だけを和集合に追従させる（どの台からも見えなくなったら解放 =
     ISSUE-0025 の修正は維持）。ボードが空になっても番号は振り直さない。
   - **`reset_board_positions()` を追加**（public）。位置・記憶・board reader のデバウンス状態を
     落とす。ハンド開始時に盤上に残っているカードは改めて 1 番から検出し直す。
2. `integration/engine.py`
   - `IntegrationThread(on_new_hand=...)` フックを additive 追加（既定 None = 従来動作）。
     `_start_new_hand` が `_board_positions` を空にするのと**同じ場所**で呼ぶ。
     フックの例外はハンドを止めない（log のみ）。
3. `main.py`: `run_cli` / `run_gui` の両方で `on_new_hand=rfid_thread.reset_board_positions` を結線
   （RFID 無効時は None）。HTTP receiver にはこのメソッドが無いので `getattr` で任意扱い。

### 併せて追加した診断（本 issue の切り分けが名前だけのログでは不可能だったため）

- engine の board ログに **`tag=` を追加**（`Board card [pos=1]: Kc (tag=E0:04:…)`）。
  「同じ札が 2 か所」が *同一 UID の二重割り当て* か *`rfid_cards.json` の重複登録*（同じカード名に
  2 つの UID）かをログだけで判定できるようにした。
- **ボードに同じカードが 2 枚以上あれば WARN + `needs_review`**（`_warn_duplicate_board_cards`）。
  1 組のデッキでは物理的にあり得ないので、黙って進めない。`register_cards.py list` への誘導も出す。

## Regression Test

`tests/test_rfid.py::TestBoardGroupPositions`:

- `test_empty_board_keeps_positions_until_new_hand` — 3 枚 → 全部下げる → **別の順で置き直しても
  それぞれ元の位置**（1 から振り直さない）。
- `test_reset_board_positions_starts_from_one_again` — `reset_board_positions()` 後は盤上に
  残っていても 1 番から振り直す / 置き直す順が違えば番号も入れ替わる（記憶を持ち越さない）。
- 旧 `test_empty_board_resets_positions_for_next_hand` は**本 issue で挙動を変えたため削除**
  （上の 2 本が後継）。

## Affected Files

- `rfid/reader_thread.py` / `integration/engine.py` / `main.py`
- `tests/test_rfid.py`
- `docs/contracts/rfid-usb-ccid.md`（§4 の「記憶クリア」条件を新ハンドに変更）

## Open / 運用上の注意

- **ハンドが終わったらボードを下げ、`n`（新ハンド）を押す**。この順序なら engine / RFID が
  必ず揃う。`n` を押さずにボードを入れ替えると、位置は記憶から復元されるので同じ札が並ぶことは
  なくなったが、engine の board は前のハンドのカードを保持し続ける（仕様どおり）。
- **`rfid_cards.json` の重複登録**（同じカード名に 2 UID）は本 fix では防げない。上の WARN で
  検出できるようにしたので、出たら `python tools/register_cards.py list` で確認する。

## Related

- ISSUE-0025（同じ UID の位置再割り当て。本 issue はその修正後に残った別要因）
- ISSUE-0024 / ADR-0042（board を 1 論理ボードにした設計。Alternatives 4 =「engine → RFIDThread の
  明示リセット」を本 issue で実装した）
- 契約 `docs/contracts/rfid-usb-ccid.md` v1.3 §4
