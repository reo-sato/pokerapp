# 2026-09-11 board reader を 1 つの論理ボードに + マイク無しでハンド読み取りを回せるように

## Goal

実プレイ環境での通し確認を始める直前に 2 つの要望・発覚があり、その両方に対応する。

1. **board reader の取り違え（ISSUE-0024）**: 契約 v1.1/v1.2 は board reader を「ストリート専用」
   （`index` + `cards`、flop は 1 台に 3 枚重ね）としていたが、実機は **ボード領域に 3 台が
   並んでいるだけ**。ユーザー指摘: 「基本的にフロップ用リーダーとかじゃなくて、3 枚リーダーが
   並んでるだけで、むしろフロップの 2,3 枚目は真ん中のリーダーの方が読みやすい」。
2. **マイク無しでハンド読み取りを優先したい**: 「マイクはなしで、ダミーのアクション情報を
   入れてもいいので、ハンド読み取りを優先して、実際のプレイ環境でのテストを行いたい」。

## Changed files

### board を 1 つの論理ボードに（ADR-0042 / 契約 v1.3 §4）

- `rfid/reader_thread.py`
  - `_board_offsets`（reader_id → {uid: offset}）/ `_board_offset_memory` を廃止し、
    **`_board_index_by_uid`（uid → 1..5）をスレッド全体で 1 つ**持つ。
  - `_board_index_for(cfg, reader_id, uid)` → `_assign_board_index(uid)`。位置は
    **board reader 全台を通した検出順**で空き最小を割り当てる。容量は `_BOARD_MAX_CARDS = 5`。
  - `_release_board_offsets` → `_release_board_indexes`。**ボードが 0 枚になったら記憶をクリア**
    （= ハンドの切れ目。次の flop 1 枚目が前ハンドの位置を継がない）。
  - `_warn_obsolete_board_fields`: 旧 config の `index` / `cards` を起動時に WARN（無視する旨）。
  - module docstring / `__init__` docstring / config 例を v1.3 に更新（board は左から右の順に書く）。
- `config_default.json`: `pcsc_readers` の board 3 件から `index` / `cards` を削除。
  `_pcsc_cards_comment` → `_pcsc_board_comment`（新モデルの説明に差し替え）。
- `tools/probe_pcsc.py`
  - lint: `_lint_board_cards`（位置の重なり検査）を廃止 → `_lint_board_obsolete_fields`（廃止
    フィールドの指摘）+ `_lint_board_group`（**board reader 1 台だけの構成を警告** = 5 枚を 1 台に
    重ねる給電不足の懸念）。
  - `_role_label`: `cards` の範囲表示（`board 1-3`）を削除。`index` は**残す** — これは
    `format_event` が `RFIDEvent.board_index` を渡す経路で「実際に割り当てられた位置」を出すため
    （config 由来の board reader は位置を持たないので `board` と出る）。

### マイク無し + ダミーアクション

- `main.py`
  - `_make_audio_thread(cfg, audio_queue, stop_event)` を追加。`config.audio.enabled`（既定 true）が
    false なら **None を返す = AudioThread を起動しない**。`run_cli` / `run_gui` の 2 か所の
    AudioThread 直生成をこれに置換し、`start` / `join` を None 安全に。
  - `run_cli` の入力ループ: **未知のコマンド行を「ディーラーの読み上げ文」として `parse_action` に
    通す**。認識できれば AudioEvent として `audio_q` に積み、できなければ使い方を表示。
    音声と同じ関数・同じ語彙なので **CLI 側に独自パーサを持たない**（二重管理の回避）。
  - 起動時の案内に読み上げ文の例（チェック / シート3 コール / ベット 500）を追記。
- `gui/dashboard.py`: `start_threads(audio_thread=...)` を `Optional` にし、None なら start しない。
- `config_default.json`: `audio.enabled`（true）+ `_enabled_comment` を追加。

### tests

- `tests/test_rfid.py`: `TestBoardStackPositions` → **`TestBoardGroupPositions`**（7 ケース）。
  本命は `test_positions_are_shared_across_board_readers`（左 1 枚 + 真ん中 2 枚の flop → 1,2,3 /
  turn を真ん中の 3 枚目に載せても 4 / river を右の台で 5 / 全台から下げたら次は 1）。
  `test_board_role_maps_to_event` の期待も `index: 3` → 検出順 1 に修正。
- `tests/test_tools_probe_pcsc.py`: `TestLintBoardCards` → **`TestLintBoardGroup`**（6 ケース）。
  `TestReaderLabel` は「config 由来は位置なし / event 由来は実位置」を分けて固定。
- `tests/test_tools_register_cards.py`: `_READERS` の board 要素と `--reader` セレクタを更新
  （`board 1-3` → `board`）。
- `tests/test_main_audio_optional.py`（新規, 8 ケース）: `_make_audio_thread` の on/off/既定/
  設定引き渡し + CLI が使う読み上げ語彙が `parse_action` を通ること。

### docs

- `docs/issues/0024-board-readers-are-not-per-street.md`（新規, Fixed）
- `docs/adr/0042-board-readers-share-one-logical-board.md`（新規, Accepted）
- `docs/contracts/rfid-usb-ccid.md` → **v1.3**（header / §3 / §4 / §10）
- `CLAUDE.md` / `CHANGELOG.md` / `docs/decision-log.md`

## Expected vs implemented

| 期待 | 実装 | 一致 |
|---|---|---|
| flop が複数台に散っても 1,2,3 | 全台共通の検出順で割り当て | ✅ |
| turn / river がどの台でも 4 / 5 | 同上 | ✅ |
| 外して戻せば同じ位置 | `_board_index_memory` | ✅ |
| ハンドを跨いだら 1 から | ボード 0 枚で memory クリア | ✅ |
| 旧 config が黙って効かない状態を作らない | 起動 WARN + lint 指摘 | ✅ |
| マイク無しで起動できる | `audio.enabled=false` → AudioThread なし | ✅ |
| ダミーアクションを打てる | 未知の行を `parse_action` へ | ✅ |

## Test results

- `pytest tests/ -q --ignore=tests/test_vision.py` → **829 passed**（board group 7 + audio 8 を追加）。
- firmware は無改修（本タスクは host のみ）。

## Mismatches / fixes（作業中に見つけたもの）

1. `_role_label` から `index` を消したら **watch の event ラベルが `board` になり位置が見えなくなった**
   （`format_event` は `RFIDEvent.board_index` を `index` として渡す）。config 由来と event 由来で
   同じ関数を使っているため。→ `index` の表示は残し、`cards` の範囲表示だけ削除して解決。
2. `config_default.json` を `json.dumps` で書き戻したら **ファイル全体が再整形**された（`blinds` や
   `resolution` のインライン記法が崩れた）。→ revert して該当行だけ文字列置換。

## Remaining gaps

- **実機での位置割り当て確認**（次の通し）: flop を「左 1 枚 + 真ん中 2 枚」で置いて
  `board 1 / 2 / 3`、turn → 4、river → 5 になること。`cards=1 を超える` WARN が出なくなること。
- **ハンドの切れ目の扱い**: RFID 側のリセットは「ボードが 0 枚」に暗黙依存する。ボードを片付けずに
  次のハンドを始めると RFID 側の位置記憶が残る（engine の `_board_positions` は新ハンドでクリアされる）。
  運用手順に「ハンド終了でボードを下げる」を入れる。engine → RFIDThread の明示リセットは未実装
  （ADR-0042 Alternatives 4）。
- flop 内の左右順（1 台に同時に載ったぶん）は UID 順で不定 = 既知の制約（ISSUE-0024）。

## 追記（同日）: 初回実機通しで見つかった位置の再割り当てバグ（ISSUE-0025）

`6ddda7d` を 11 台実機で通したところ、**新モデルは動いた**が 2 つの実装バグが出た。

良かった点:

- `Board card [pos=1] → [pos=2] → [pos=3]` → `Street auto-advanced to flop` ＝ **位置の連番と
  ストリート自動遷移が動作**。
- キーボードのダミーアクションが全部通った（`チェック` / `ベット５`（全角数字も可）/ `コール`）。
- `w 1` → `hole_cards from RFID — {1: ['Qc','Jc'], 2: ['9c','Kc']}` → JSON 保存まで通し。
- 席の読み取りは 2 枚ずつ完璧。

バグ:

```
[pos=2]: 5c  →  [pos=4]: 5c   （同じ札が 2 位置 → turn 誤発火）
[pos=3]: 6c  →  [pos=2]: 6c   （位置が移動）
```

原因は (a) **既に位置を持つ UID を再割り当てしていた**（`taken` に自分の位置が含まれるので
`index in taken` が必ず真）、(b) **位置の解放が reader 単位**だった（隣接リーダーの磁界が
重なると 1 枚を 2 台が読み、片方から消えただけで位置を失う）。詳細は ISSUE-0025。

修正:

- `_assign_board_index`: 既に位置を持つ UID は**その位置を返す**（先頭で短絡）。
- `_sync_board_presence`: `_board_uids`（board reader ごとの現在 UID 集合）の**和集合**で
  解放を判定。和集合が空になったときだけ記憶をクリア。`_release_board_indexes` は廃止。
- 契約 v1.3 §4 に「1 枚 = 1 位置」「解放は和集合」の 2 MUST を追記。

回帰テストは **2 つの修正をそれぞれ単独で外すと落ちる**ことを確認済み
（`test_same_card_seen_by_two_readers_keeps_one_position`）。831 passed。

**学び**: 「同じ UID が複数 reader から発火し得る」ことは ADR-0042 の時点で分かっていた
（デバウンスは reader 単位）はずなのに、位置割り当て側でその経路を考慮していなかった。
UID をキーにする設計を選んだ以上、**同一 UID の再入は冪等**でなければならない。

## 追記 2（同日）: 位置リセットの同期点を新ハンドに（ISSUE-0026）+ 実機ログからの所見

`da17e7a`（ISSUE-0025 修正）で「同じ UID の再発火が同じ位置になる」ことは実機で確認できた
（`[pos=2]: Qc` が 4 回、すべて 2 番）。しかし **異なる位置に同じカード名**が残った:

```
[pos=3]: Jc → [pos=4]: Jc   board ['Kc','Qc','Jc','Jc']
[pos=2]: Qc → [pos=3]: Qc   board ['Kc','Qc','Qc','Jc']
```

原因は **engine の board は縮まない**（`_handle_board_rfid` は埋めるだけ・外れてもイベント無し =
ハンド確定までボードを保持する設計）のに、**RFID 側はボードが空になった時点で位置を振り直して
いた**こと。ボードを一度下げて別の順で置き直すと両者がずれる。詳細は ISSUE-0026。

修正: **位置リセットは新ハンドだけを同期点にする**。

- `RFIDThread.reset_board_positions()`（新規 public）: 位置・記憶・board reader のデバウンスを落とす。
- `IntegrationThread(on_new_hand=...)`（additive, 既定 None）を `_start_new_hand` の
  `_board_positions = {}` と同じ場所で呼ぶ。フックの例外はハンドを止めない。
- `main.py` の `--cli` / GUI 双方で結線（HTTP receiver にこのメソッドは無いので `getattr` で任意扱い）。
- `_sync_board_presence` は記憶を捨てず、いま盤上にある UID だけ和集合に追従（ISSUE-0025 の修正は維持）。

診断も 2 つ追加した（本 issue の切り分けが名前だけのログでは不可能だったため）:

- board ログに **`tag=`（UID）** を出す → 「同じ札が 2 か所」が同一 UID か
  `rfid_cards.json` の重複登録（同じカード名に 2 UID）かをログだけで判定できる。
- **ボードに同じカードが 2 枚以上で WARN + `needs_review`**（1 組のデッキでは物理的にあり得ない）。

旧テスト `test_empty_board_resets_positions_for_next_hand` は本 issue で挙動を変えたので削除し、
`test_empty_board_keeps_positions_until_new_hand` / `test_reset_board_positions_starts_from_one_again`
に置き換えた。832 passed。

### 実機ログからのその他の所見（本タスクでは未対応）

- **pokerkit が未導入**と見られる: JSON の `"pots": []` と全 action の `confidence: 0.5`
  （= legacy の固定表「audio のみ」）。`engine.backend="pokerkit"` は既定だが未導入だと warning を
  出して legacy にフォールバックする（CLAUDE.md エラーハンドリング方針）。この状態では
  **actor 推定 / 合法手射影 / silent-fold 合成 / side-pot / 派生 confidence が全部無効**。
  → `pip install pokerkit` を Phase H の残作業に追記。
- `b 5` と打って認識されなかった（ヘルプどおり `ベット 5` は通る）。英字・ローマ字の別名は未実装。
- turn で `席1 bet 5` → `席2 bet 10` と記録された（本来は raise）。legacy backend は読み上げを
  そのまま記録するため。rules-aware backend なら合法手射影で矯正される見込み。

## Related

- ISSUE-0024 / ADR-0042 / 契約 `rfid-usb-ccid.md` v1.3
- ISSUE-0021（poll 周期。`cards` は v1.1 でここから入った）
