# 2026-09-25 読んだ札の表示・数字だけのベット・チェックアラウンド（店舗の 3 回目の通しテスト）

## Goal

店舗の 3 回目の通しテスト（CLI 出力 + `events.jsonl`）を受けて:

1. CLI のログに RFID で読んだハンドの情報（手札・ボード）も出す（オーナーの依頼）。
2. ベット・レイズは語を言わなくても、額を言えばアクションとして読む（オーナーの依頼）。
3. 「チェックアラウンド」はプレイヤー全員がチェックしたこと（オーナーの説明）。

## 実測（1 ハンド目, ボタン 席6 / SB 席4 / BB 席5）

- 手札: 席4 6h Qd / 席5 6c 7c / 席6 9h 6s。ボード 3d Js 7h / 5h / Td。
- 「600点」（フロップのベット）と「2千点」（リバーのベット）が「アクションとして読めず」→ 続く「コール」が
  チェックになり、以降の手番がすべてずれた。
- 「コール」のあとの「600点コールです。」は話し始めがターンの札の約 3 秒前（言い直し）→ ターンのチェックに
  なっていた。
- 「チェックアランド、ラストカード」が 1 人分のチェックにしかならなかった。
- ショーダウン後の会話と、プロンプトの語を順に並べる幻聴（「シート4 レイズ 2千、コール、チェック、フォールド、
  オールイン、…」）がリバーのレイズ 2000 として記録された。

## Changed files

- `audio/recognizer.py`: `parse_amount_only`（額だけの発話 → action="bet" + flag `amount_only`。前後に付いて
  よい語は「点・です・はい・アクション」等だけ。違う数が並ぶ発話は不可）/ `_split_off_amounts`（コール・
  チェック・フォールドの前後に「、」で区切った額を別の人のベットに）/ `check_around` flag / `is_prompt_echo`
  にプロンプトの語 4 つの並びと字幕の定型句。
- `audio/recorder.py`: `describe_event` が `bet/raise 600 （数字だけ）` / `check 全員` と出す。
- `integration/engine.py`: `on_cards`（`[カード]` 行: 手札・フロップ / ターン / リバー・差し替え・確定時の
  まとめ）/ 数字だけの発話の受付（`_amount_only_problem`: レイズはいまのベットより大きい額、ベットは最小
  ベット以上。使えなければ `●` で理由）/ `_handle_check_around` / 前のストリートのコール・チェックアラウンド
  を捨てる（`_said_before_street`: いまのストリートの最初の札の時刻より 1 秒以上前に話し始めた、またはボードを
  RFID で読んでいるハンドのターン・リバーで札がまだ見えていない）/ `amount_only`・`check_around` は review の
  理由にしない。
- `main.py`: `_print_cards`（`[カード]`）、キーボード入力の確認表示を `describe_event` にそろえた。
- `docs/contracts/schemas/reconstruction_event.schema.json`: parse_flags の enum に `too_many_actions`
  （ADR-0061 で入れ忘れ）/ `amount_only` / `check_around`。
- `tests/test_spoken_amounts.py`（新規 45）: 実測のハンドをそのまま流して正しく記録できること、ほか。

## Expected vs implemented

実測のハンドを流すと: preflop call(6) / fold(4) / raise 1000(5) / call 800(6) → flop bet 600(5) / call(6)
→ turn check / check → river bet 2000(5) / call(6) →「ハンド終了」で手札から 席5（ワンペア）の勝ち、
ポット 7300。言い直しのコールと「7」は記録しない。

## Tests

- `tests/test_spoken_amounts.py` 45 passed。全体 1490 passed / 5 skipped、ruff clean。
- `tests/test_cli_session_layer.py`: キーボード入力の確認表示の形（`→ call 席2`）に合わせた。

## Remaining gaps

- 数字だけの発話は、いまのベットより大きければレイズとして記録する。コールの額を**違う額で**言い直した場合
  （聞き違い）はレイズになる。
- 前のストリートの判定はボードの札の時刻が頼り。ボードを読めていないフロップでは使えない（従来どおり
  コール → チェック + 要確認）。
- ADR / ISSUE / CLAUDE.md / usage は動きが固まってからまとめて更新する（CLAUDE.md §7b）。
