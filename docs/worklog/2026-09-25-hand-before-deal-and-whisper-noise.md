# 2026-09-25 配る前に `n` で始めたハンド・雑音への長い書き起こし（店舗の 5 回目の通しテスト）

## Goal

店舗の 5 回目の通しテスト（セッション `c9e150f3141144fd939bf4ca2e48c2a5`: pokerapp.log / events.jsonl /
table_state.jsonl / transcripts.jsonl / ハンドの JSON）で「変な書き出しが出ちゃってる」（オーナー）。

## 実測（ハンド 1, ボタン 席6 / SB 席4 / BB 席5）

- 23:06:11〜18 にボードのリーダーの上で札を混ぜ（RFID は 5 枚を確定したが、ハンドが無いので engine は無視）、
  23:06:19 に `n`（ハンド 1 開始、プレー中）。
- 配るまで卓が空 → 23:06:38（`n` の 19 秒後）に「プレー中: いいえ（次の配布まで音声を聞き流す）」。
  `_check_table_cleared` が「確定していないハンドの片付け」とみた。
- 23:06:40〜45 に配布（席4 Jd 2s / 席5 5s 6h / 席6 8s Qd）。ハンドは開いていて何も起きていないので、札は
  いまのハンドの手札になった（配布の検出は動かない）→ **プレー中に戻らない**。
- flop Tc 7c 5d / turn 4h / river 3h を RFID は確定したが、engine は「プレー中でないボード」として無視 →
  `board: []`。聞き流しのままなので、ハンド中の声は 1 件も書き起こされていない（transcripts.jsonl の 7 件は
  すべて `n` から配布までの 18 秒間に話し始めたもの）。
- 札の離脱: 席4 はフロップで 23:07:20、席6・席5 はショーダウンで 23:07:42〜46 に札を前に出した。ボードが
  無いのでストリートはプリフロップのまま → 席6・席4 のフォールド、席5 の勝ち（フォールド）。ポットは 300、
  pots が `[100 eligible 5], [200 eligible 5]`。本当は 3〜7 のストレートで席5 の勝ち。
- 書き起こし 7 件のうち 3 件はプロンプトの繰り返し（「シート4 レイズ 2千、コール、チェック、フォールド、
  オールイン、ショーダウン、ウィナー、ハンド、…」を 448 トークンまで）で、1 件 15〜18 秒。3 件で認識が
  約 50 秒遅れ、札の離脱の反映も待たされた。CLI には数百文字の文がそのまま出た（「変な書き出し」）。
  1 件は「ご視聴ありがとうございました。」。

## Changed files

- `integration/engine.py`
  - `_check_table_cleared`: まだ何も起きていないハンド（`_hand_in_play()` が偽 = アクション・ボードが無い）
    の空の卓は片付けにしない（配る前・配り直し）。
  - `_handle_board_rfid`: 手札が 1 枚も届いていないハンドのボードの札は読まない（`_waiting_for_deal`、席の
    リーダーがつながっているときだけ）。読まなかった札があれば、最初の手札が届いたときに RFID のボードの
    位置を捨てる（`_forget_board_before_deal` → `on_new_hand`。RFID はハンドの中で位置を解放しないので、
    捨てないと本物の flop が 4 枚目から数えられる）。
  - `_seat_readers_live` / `_waiting_for_deal`。
- `audio/recognizer.py`
  - `WhisperTranscriber.recognize()` → `Recognition(text, confidence, no_speech)`。`transcribe_with_confidence`
    は後方互換で残す。
  - `has_speech`: faster-whisper 同梱の Silero VAD で声が無い音を Whisper にかけない（`vad_threshold`,
    `min_speech_duration_ms=100`）。VAD が動かなければ以後は使わない（従来どおり全部かける）。
  - `max_new_tokens = min(200, 40 + 20 × 秒)`（faster-whisper 1.0 以降にある引数）。
  - `is_implausibly_long(text, audio_sec)`: 空白を除いて `12 + 12 × max(1, 秒)` 文字を超える書き起こしは
    雑音（人は 1 秒に 10 文字も言わない。打ち切ったループは必ずこれを超える）。
- `audio/recorder.py`: `recognize` があれば使う。声が無い音は `Transcript(no_speech=True, noise=True, text="")`
  を報告して終わり。雑音 = プロンプトの繰り返し or 長すぎる。`_report`。
- `main.py`: `audio.vad_threshold` を渡す。`_print_transcript` は声が無い音を出さず、雑音は頭 20 文字だけ。
- `output/transcript_log.py`: `no_speech`。
- `tools/audio_check.py`: listen に「声ではない音 N 秒 — Whisper にかけず」と件数、`--vad-threshold`。
- `config_default.json`: `audio.vad_threshold` 0.5（+ コメント）。
- `tests/test_hand_before_deal.py`（新規 8）: セッション c9e150f3 の札の時刻を流す（修正前は 5 件失敗）。
  `tests/test_whisper_noise.py`（新規 19）。`tests/test_play_gate.py`: `__new__` で作る transcriber に
  `_vad_threshold`。

## Expected vs implemented

セッション c9e150f3 の札の時刻をそのまま流すと: ボード Tc 7c 5d 4h 3h を記録、preflop は札が残っている人の
コール / チェックで閉じ（要確認）、席4 はフロップのフォールド、ターン・リバーはチェック、リバーで札を前に
出した席5・席6 はチェック（ショーダウン）→ 次の配布で手札から 席5 の勝ち（ストレート, `winner_source=cards`）。
`n` から配布までの 26 秒間もプレー中のまま（聞き取りを続ける）。

## Tests

- 新規 27 件 passed。全体 1548 passed / 5 skipped（pwsh が無い環境の installer テスト）、ruff clean。

## Remaining gaps

- VAD の閾値 0.5 は店舗の音で確かめていない。短い言葉が「声ではない音」になっていないかを `audio_check listen`
  と transcripts.jsonl（`no_speech: true` の行、`audio.save_audio` なら WAV）で確かめる。
- 声として聞こえる会話がプロンプトの繰り返しになる場合は残る（上限で 5 秒前後に短くなるだけ）。プロンプトの
  書き換え（語の列挙をやめる）は、雑音の見分けがプロンプトの語の並びに頼っているので保留。
- `n` から配るまでの会話は書き起こされる（プレー中のまま）。アクションの語を含む会話は記録に入りうる。
- 手札が 1 枚も読めないハンド（席のリーダーの故障）で `n` を使うと、ボードの札を読まない。
- ADR / ISSUE / CLAUDE.md / usage は動きが固まってからまとめて更新する（CLAUDE.md §7b）。
