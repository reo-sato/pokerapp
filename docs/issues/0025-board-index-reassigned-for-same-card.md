# Issue 0025: 同じ札に別の board 位置が再割り当てされ、turn が誤発火する

## Date

2026-09-11

## Status

Fixed（`rfid/reader_thread.py`。実機での再確認は次の通しで）

## Severity / Priority

- Severity: High（ストリートが実際より先に進む = ハンドログが壊れる）
- Priority: P1（ADR-0042 の初回実機通しで発覚）

## Area

rfid / host（`rfid/reader_thread.py`）

## Expected Behavior

1 枚の物理カードは **ボード上で 1 つの位置しか占めない**。同じ UID が何度検出されても
`RFIDEvent.board_index` は同じ値で、engine は同じスロットを上書きするだけ。ストリートは
**実際に増えた枚数**（3 / 4 / 5）でのみ進む。

## Actual Behavior

実機 2026-09-11（`6ddda7d`, 11 台, flop を左 1 枚 + 真ん中 2 枚）:

```
18:09:41,889 Board card [pos=1]: 7c — board ['7c']
18:09:41,890 Board card [pos=2]: 5c — board ['7c','5c']
18:09:41,890 Board card [pos=3]: 6c — board ['7c','5c','6c']
18:09:41,891 Street auto-advanced to flop (3 cards detected)        ← ここまで正しい
18:09:42,908 Board card [pos=4]: 5c — board ['7c','5c','6c','5c']   ← 同じ 5c が 4 番にも
18:09:42,909 Street auto-advanced to turn (4 cards detected)        ← turn 誤発火
18:09:42,910 Board card [pos=2]: 6c — board ['7c','6c','6c','5c']   ← 6c が 3 番 → 2 番へ
```

- **同じ札が 2 つの位置を占める**（`5c` が 2 番と 4 番、`6c` が 3 番と 2 番）。
- 枚数が水増しされ、**flop を置いただけで turn に進む**。
- その後も位置 2 が `6c` → `Tc` → `8c` と上書きされ続けた。

## Reproduction

board reader を 2 台以上、**磁界が重なる間隔**で並べ、その重なりにカードを 1 枚置く。
または 1 枚を 2 台の境界付近で少し動かす。

## Root Cause

2 つの実装ミスが重なっている。どちらも ADR-0042（board 全台で 1 論理ボード）の実装時に入れた。

### (a) 既に位置を持つ UID を再割り当てしていた

```python
taken = set(self._board_index_by_uid.values())
index = self._board_index_memory.get(uid)
if index is None or index in taken:      # ← uid 自身の位置も taken に入っている
    index = next(free)                   # ← なので必ず別の位置を取り直す
```

`_board_index_by_uid` に既に載っている UID を再評価すると、**自分自身が占めている位置**が
`taken` に含まれるため条件が真になり、毎回新しい位置を割り当てていた。

### (b) 位置の解放が reader 単位だった

`_release_board_indexes(removed)` は `prev - current`（**その reader から消えた UID**）を
グローバルな位置表から削っていた。**隣接リーダーの磁界が重なって 1 枚を 2 台が読む**構成では、
片方の台から見えなくなっただけで位置を失い、次にどちらかが再検出したときに「新規 UID」として
別の位置を取ってしまう。

### なぜ同じ UID が複数回発火するのか（前提）

デバウンス（`_last_uids`）は **reader ごと**に持つ。これは正しい（`RFIDEvent.reader_id` は
reader 単位の情報）。1 枚が 2 台の磁界に入っていれば、2 台それぞれが「新しく増えた UID」として
1 回ずつ発火する = **同じ UID で 2 イベント**が正常に起こり得る。ADR-0042 の位置モデルは
UID をキーにしているので、これは本来 **無害**（同じ位置を返して engine が上書きするだけ）
であるべきだった。

## Fix

`rfid/reader_thread.py`:

1. **既に位置を持つ UID はその位置を返す**（`_assign_board_index` の先頭で短絡）。
   同じ札を 2 台が報告しても位置は 1 つに保たれ、engine は同じスロットを上書きする。
2. **位置の解放は全 board reader の UID 和集合で判定**する（`_sync_board_presence`）。
   `_board_uids: dict[reader_id, set[str]]` を board reader だけ更新し、和集合から消えた UID の
   位置のみ解放する。**和集合が空になったら記憶もクリア**（= ハンドの切れ目）。
   席 reader の抜き差しは board の位置に影響しない（role で分岐）。

`_release_board_indexes` は廃止（`_sync_board_presence` に統合）。

## Regression Test

`tests/test_rfid.py::TestBoardGroupPositions`:

- `test_same_card_seen_by_two_readers_keeps_one_position` — 左 1 枚 + 真ん中 2 枚の flop →
  1,2,3 / 真ん中の台も 1 枚目を拾い始めても **同じ位置 1 で再発火するだけ** / 左から消えても
  真ん中が見ているので解放しない / 次の新札は 4（turn として正しい）/ 全台から消えて初めて
  解放 + 記憶クリア → 次は 1。**修正 (a) を外すと落ち、(b) を外しても落ちる**ことを確認済み。
- `test_seat_reader_removal_does_not_clear_board_memory` — 席のカードの抜き差しが board の
  位置に影響しないこと。

## Affected Files

- `rfid/reader_thread.py`
- `tests/test_rfid.py`
- `docs/contracts/rfid-usb-ccid.md`（§4 に「同じ UID は同じ位置」「解放は和集合」を明記）

## Related

- ADR-0042 / ISSUE-0024（board を 1 論理ボードにした変更。本 issue はその実装バグ）
- 契約 `docs/contracts/rfid-usb-ccid.md` v1.3 §4
