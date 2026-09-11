# Issue 0024: board reader を「ストリート専用」と取り違えていた（flop 3 枚重ね前提）

## Date

2026-09-11

## Status

Fixed（host 実装 + 契約 v1.3 + ADR-0042。実機での位置割り当て確認は次の通しで）

## Severity / Priority

- Severity: High（ボード位置が実際の配置と一致せず、`cards` 超過 WARN が常時出る）
- Priority: P1（実プレイ環境での通し確認の直前に発覚）

## Area

rfid / host（`rfid/reader_thread.py`, `config_default.json`, `tools/probe_pcsc.py`）

## Expected Behavior

コミュニティカード 5 枚は **ボード領域に並べた board reader 群**の上に置かれる。どのカードが
どの台に載るかは **置き方次第**（flop 3 枚が 3 台に散ることも、真ん中の 1 台に 2 枚載ることも
ある）。`RFIDEvent.board_index` は「ボード全体で何枚目か」= flop 1/2/3・turn 4・river 5 を表し、
物理的にどの台が読んだかには依存しない。

## Actual Behavior

契約 v1.1/v1.2 と host 実装は **「1 台 = 1 ストリート専用」** を前提にしていた:

```jsonc
{"reader": 8,  "role": "board", "index": 1, "cards": 3},  // flop 3 枚を 1 台に重ねる
{"reader": 9,  "role": "board", "index": 4},              // turn 専用
{"reader": 10, "role": "board", "index": 5}               // river 専用
```

`board_index` は **reader ごとに独立**して `index + offset`（offset = その台での検出順）で
決まっていた。実機の配置は「3 台が並んでいるだけ」なので、この前提が崩れると:

- **turn の台に 2 枚目が載ると `cards=1` 超過**で `board_index=None`（位置なし記録）。
  2026-09-11 の `probe_pcsc watch` で実際に発生:
  `Board reader reader_9: cards=1 を超えるカード（tag=…B7:18）— board_index なしで記録します`
- **flop を 3 台に散らすと位置が飛ぶ**: 左の台（index 1）に 1 枚 → 1、真ん中の台（index 4）に
  2 枚 → **4, 5** になり、「flop 3 枚 = 1,2,3」にならない。engine の street 自動遷移は
  `_board_positions` の枚数で判定するため枚数自体は数えられるが、**turn / river の位置が
  埋まってしまい**、実際の turn が 6 枚目扱い（= 5 枚超過で WARN + 位置なし）になる。
- ユーザーの指摘（2026-09-11）: 「基本的にフロップ用リーダーとかじゃなくて、3 枚リーダーが
  並んでるだけで、むしろフロップの 2,3 枚目は真ん中のリーダーの方が読みやすい」。

## Reproduction

1. `config.rfid.pcsc_readers` を上記（v1.2 サンプル）のままにする。
2. `python tools/probe_pcsc.py watch --seconds 40`
3. flop の 1 枚目を reader 8、2・3 枚目を reader 9 に置く。
   → `board 1` / `board 4` / `board 5` になり、turn を置くと `cards=1 を超える` WARN。

## Root Cause

**契約の設計時に物理配置を確認しなかった**。v1.1 で「1 リーダー複数枚」を導入したとき、
複数枚が必要な理由を「flop 3 枚を 1 台に重ねる」と解釈し、`index`（そのリーダーの先頭ボード位置）
+ `cards`（重ねる枚数）という **reader 単位の位置モデル**を契約に固定した（`rfid-usb-ccid.md`
v1.1 §4）。実際は 5 枚 / 3 台なので、どの台が何枚受けるかは固定できない。

なお「1 リーダーに複数枚」の**読み取り機構自体は正しかった**（席の hole card 2 枚、および
board の 1 台に 2〜3 枚）。誤っていたのは **位置の決め方**だけ。

## Fix

**ADR-0042**: board reader 全台を **1 つの論理ボード**として扱い、`board_index` は
**全台を通した検出順**（= ディーラーが配った順）で 1..5 を割り当てる。契約 **v1.3 §4**。

- `rfid/reader_thread.py`: `_board_offsets`（reader_id → {uid: offset}）を廃し、
  `_board_index_by_uid`（uid → 1..5）をスレッド全体で 1 つ持つ。容量はボードの 5 枚。
- **ボードが 0 枚になったら位置記憶をクリア**（= ハンドの切れ目。次の flop 1 枚目が前ハンドの
  位置を継がない）。1 枚だけ浮かせて戻す間は他の札が残っているので記憶が効き、並びは変わらない。
- config から board reader の `index` / `cards` を削除。残っていれば **起動時に WARN して無視**
  （`_warn_obsolete_board_fields`）+ `probe_pcsc check` の lint でも指摘する。
- lint は「位置の重なり」検査（概念が消えた）を捨て、代わりに **board reader 1 台だけの構成**を
  警告する（5 枚を 1 台に重ねることになり給電不足で読めない可能性が高い）。
- `probe_pcsc` の reader ラベルは config 由来では `board`（位置なし）、**RFIDEvent 由来では
  `board 3` のように実際に割り当てられた位置**を出す（`_role_label` は `index` があれば表示）。

### 既知の制約（Fix 後も残る）

**同じ poll で 2 枚以上増えたとき、その中の左右順は保証しない。** 台をまたぐぶんは config の
記載順（= 左から右に並べて書く規約）で決まるが、**1 台に同時に載った複数枚の順序は UID 順**で
物理的な左右とは無関係。実害は小さい:

- street 自動遷移は**枚数**で判定するので影響なし。
- PHH は flop 3 枚をまとめて出力する（順序を持たない）ので影響なし。
- 影響するのは JSON ログの board 配列の並びだけで、flop 内の左右が入れ替わり得る。

## Regression Test

`tests/test_rfid.py::TestBoardGroupPositions`（旧 `TestBoardStackPositions` を置換）:

- `test_flop_three_cards_get_positions_1_2_3` — 1 台に 3 枚 → 1,2,3
- `test_positions_are_shared_across_board_readers` — **本命**: 左に 1 枚 + 真ん中に 2 枚の flop
  → 1,2,3 / turn を真ん中の 3 枚目として載せても 4 / river を右の台で 5 / 全台から下げたら次は 1
- `test_empty_board_resets_positions_for_next_hand` — ボード 0 枚で記憶クリア
- `test_sixth_card_over_board_capacity_warns_and_has_no_index` — 6 枚目は WARN + None
- `test_removed_card_returns_to_same_position` / `test_freed_position_is_reusable_by_another_card`
- `test_obsolete_index_and_cards_are_ignored` — 旧 config は無視 + WARN

`tests/test_tools_probe_pcsc.py::TestLintBoardGroup` — 廃止フィールドの指摘 / 1 台構成の警告。

## Affected Files

- `rfid/reader_thread.py`
- `config_default.json`
- `tools/probe_pcsc.py`
- `tests/test_rfid.py` / `tests/test_tools_probe_pcsc.py` / `tests/test_tools_register_cards.py`
- `docs/contracts/rfid-usb-ccid.md`（v1.3）

## Related

- ADR-0042（本 issue の fix 設計）/ ADR-0034（契約 freeze）/ ADR-0041（1 slot + P2）
- ISSUE-0021（poll 周期。`cards` は v1.1 でここから入った）
