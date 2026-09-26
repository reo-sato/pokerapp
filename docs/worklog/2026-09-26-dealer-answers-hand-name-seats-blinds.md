# 2026-09-26 オーナーの回答を設計に反映（役名でハンド終了・席の参加と休み・ブラインドの変更）

## Goal

ライブポーカーの進行と卓の物理について尋ねた 15 問へのオーナーの回答（2026-09-26）を、記録の仕組みに反映する。

## 回答（要点）と反映

| 回答 | 反映 |
|------|------|
| フォールドした札は必ずしもディーラーの前（ボードのリーダー）を通らない。リーダーの反応範囲は狭い | 中央通過（muck）は早い確定の材料にとどめ、フォールドの判定は 3 秒の不在を主にする（変更なし。設計の前提を修正） |
| ブラインドは店舗 iPad かトーナメントタイマーで上げる予定 | `blinds <SB> <BB>`（CLI）/ control `set_blinds`（staff API）。engine は次のハンドから。ハンドの記録の blinds はそのハンドのもの |
| アンティ・ストラドルは後で | 未対応のまま（将来） |
| NLHE のみ、デッキ 1 組（追加登録は後で） | 変更なし |
| 席は操作でアサイン。離席（バケーション）は無い | `name <席> -` = 休み（次のハンドから配られない）/ `name <席> <名前>` = 参加。**スタック 0 の席は買い足すまで配られない**（pokerkit はスタック 0 の `create_state` を拒否 = そのまま配ると `new_hand` が例外で integration スレッドが止まっていた）。ボタンは参加席の中で回る。2 席未満なら始めずに知らせる |
| オールインの額は言わない。買い足しは操作 | 変更なし（スタックは記録の積み上げ。聞き落としがあれば要確認） |
| レイズの額はトータル | `raise_to_vs_by_ambiguous` の review を廃止（最小未満は聞き違い = snap + review） |
| ポットの額は言わない | 検算は無し |
| ショーダウンは勝った役名を言う（「ウィナー」は言わない）。見せるべきときのマックは「フォールド」 | 役名（`HAND_NAME_KEYWORDS`）= `end_hand` + `AudioEvent.hand_name`。判定と突き合わせ、違えば要確認（役名に合う席が 1 つならその席の勝ち = `winner_source: announced`）。手札が読めていない席があっても役名から決める |
| マックは散らばるが次のストリートまでに片付く。バーンはフロップの 1 枚外側 | 変更なし。バーンがボードに出たら置き場所を離す（運用） |
| 手札は置いたまま覗く。片方は読める | 3 秒の不在判定でよい |
| ショーダウンで前に出した札がボードのリーダーに載ることはまれにある | RFID の手札フィルタ + ベッティング終了後の離脱はマックにしない（既存）で吸収 |
| 席番号は時計回り | 変更なし |
| テストはディーラー 1 人 + プレイヤー役 1 人 | 次は実際のプレイヤーで |

## Changed files

- `core/constants.py`: `HAND_NAME_KEYWORDS`（役名 → pokerkit の役名）を `ACTION_KEYWORDS` に `end_hand` として合流。
- `audio/recognizer.py`: `parse_action` が役名の語から `hand_name` を載せる（額は 0）。`apply_corrections` の
  to/by 曖昧 review を廃止。
- `core/events.py`（`AudioEvent.hand_name`）/ `output/event_recorder.py` / `integration/replay.py` /
  `docs/contracts/schemas/reconstruction_event.schema.json`（0.6）。
- `core/hand_log.py`（`HandSummary.announced_hand`）/ `docs/contracts/schemas/hand.schema.json`（1.5: `announced_hand`,
  winner_source に `announced`）/ `docs/contracts/versioning-and-freeze.md`。
- `core/poker_engine.py`: `playing_seats` / `seats_in_hand` / `sit_out` / `sit_in` / `set_blinds`（`PokerEngine`
  Protocol に additive）。`new_hand` は休みでなくチップがある席だけで作り、ボタンは参加席の中で進める（前のボタンの
  席が抜けたらその次の席）。2 席未満は ValueError（状態不変）。`get_stacks` は全席、`get_active_seats` /
  `position_map` / `end_hand*` / `_snapshot_pots` はハンドの席だけ。`core/game_state.py`（legacy）にも最小実装。
- `integration/engine.py`: `_handle_end_hand`（役名 → foldout 取り消し・ベッティングを閉じる・判定）/
  `_finish_showdown`（役名との突き合わせ）/ `_finish_by_announcement`（読めない席）/ `_check_announced_after_end` /
  `_handle_sit` / `_handle_set_blinds` / `_start_new_hand` が bool（始められなければ `_refuse_start` で 1 回知らせ、
  配布は保留 = 買い足し・参加で始まる）/ ハンド開始の知らせに「休み: 席N」/ `players` は配られた席だけ /
  `_last_result["hand"]` / `_describe_result` の announced。
- `main.py`: `blinds <SB> <BB>`、`name` は session layer が無効でも席の参加・休みを反映（sit_out / sit_in を送る）。
- `core/control_queue.py` / `api/server.py` / `api/client.py`: control `set_blinds`（sb, bb）/ `sit_out` / `sit_in`（seat）。
- `audio/recorder.py`: `describe_event` が「ハンド終了（ツーペア）」。
- tests: `tests/test_hand_name.py`（新規 29）/ `tests/test_seats_and_blinds.py`（新規 17）。`test_cli_session_layer`
  （空席は players に入らない）/ `test_reconstruction_hardening`（to/by）/ `test_rfid_table_flow`（schema 0.6）。

## Expected vs implemented

- 役名: 「フラッシュ」で席6 のフラッシュが判定と一致 → 確定（review なし）。「フルハウス」（誰も無い）→ 判定の勝者 +
  要確認。「ツーペア」（席5 の手）→ 席5 の勝ち（announced, 要確認）。席6 の札が 1 枚しか読めていなくても
  「ツーペア」→ 席5、「フラッシュ」→ 席6。リバーのチェックを聞き落としたまま役名 → チェックで閉じて判定（要確認）。
- 席: `sit_out` は次のハンドから（いまのハンドはそのまま）。スタック 0 の席を飛ばして始まり、2 席未満なら
  「ハンドを始められません — …」を 1 回、`r` で買い足すと配布から始まる。
- ブラインド: ハンドの途中の変更は次のハンドから。記録の blinds と pot（SB 200 + BB 400 + コール 400 = 1000）。

## Tests

- 新規 46 件 + 更新 4 件（control queue の set_blinds / sit_out / sit_in を含む）。全体 1593 passed / 5 skipped、
  ruff clean。

## Remaining gaps

- トーナメントタイマーとの連動・iPad の画面（staff アプリ）は未着手（control API は用意した）。
- アンティ・ストラドル、2 組目のデッキ、席の「休み」の staff アプリからの操作（seat タブ）は未対応。
- 役名の語彙は Whisper の書き起こしゆれ（「ツーペアー」等）を店舗の transcripts.jsonl で確かめてから足す。
- 役名で決めた勝者（announced）の side pot は分けない（全額）。要確認で人が直す。
- ADR / ISSUE / CLAUDE.md / usage は動きが固まってからまとめて更新する（CLAUDE.md §7b）。
