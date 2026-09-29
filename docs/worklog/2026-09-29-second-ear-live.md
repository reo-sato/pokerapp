# 2026-09-29 第 2 の耳をライブの聞き取りに入れる（Whisper が読めなかった発話の聞き直し）

## Goal

`2026-09-29-second-ear-evaluation.md` の結果（Whisper が読めなかった発話だけ第 2 の耳で補うと、真のアクションとの
一致 73% → 80%）を受けて、オーナーが「次の一歩はその方針で」。あわせて確認事項の答え:

1. d0f055fb ハンド 1 のフロップの「のっぴょく」は 600 → フロップは席4 のベット 600 と席5 のコール（真のアクションは
   チェック 2 回になっていた = 記録をそのまま確かめていた）。
2. 027e4b15 ハンド 1 のフロップの「1000」と「コール」はそれでよい（真のアクションは全員チェックになっていた）。
3. 027e4b15 のリバー（「ご視聴ありがとうございました」の下の「六千五百」→「コール」）は分からない → 発話のファイルを
   オーナーが聞く（`logs/audio/027e4b15f59a4fd099654bfc3c4c626a/1790485754345.wav`）。

## Changed files

- `audio/recorder.py`: Whisper がアクションとして読めなかった発話（空・幻聴・読めない文。確認型の発話と VAD の
  「声ではない音」は除く）だけ第 2 の耳で聞き直す。第 2 の耳が自由に聞いた文そのものが、いちばん確からしい候補と
  同じアクションに読めるときだけ、その候補からアクションを作る（`second_ear` の印 = 要確認、聞き取りの自信 0.5）。
  `Transcript.ear` / `ear_text`。第 2 の耳が失敗しても聞き取りは止めない。
- `audio/second_ear.py`: `agreed_candidate`（厳しめの規則。評価と共通）、`rescue_events`、`pcm16_samples`、
  `load_live`（config `audio.second_ear` = {"enabled": true, "threads": 4}。項目が無い config でも使う。モデル・
  部品が無い / 壊れていれば Whisper だけ。読み込み時に一度聞いてみる）。
- `main.py`（CLI / GUI）・`tools/audio_check.py listen`: 第 2 の耳を読み込み、どうなったかを表示。聞き取りの行に
  「第 2 の耳「六百」→ bet/raise 600（数字だけ・第 2 の耳）」。
- `output/transcript_log.py`: 聞き直した発話は `ear`（自由に聞いた文・上位 8 候補・秒数）と `ear_text` を記録。
- `tools/eval_store.py`: 読み直しは、記録の `ear` をいまの規則で読む（ライブと同じ）。`ear_best` も同じ規則。
  fixture に `ear` を残す。**真のアクションを直したハンドは前の baseline を引き継がない**。
- `tools/second_ear.py --model-only`、`installer/install.ps1`（`Invoke-EarModel`: 更新・導入のときにモデルを取得。
  失敗しても続ける。`-SkipModel` で飛ばす）。`config_default.json`（`audio.second_ear`）。
- `shared/hand_replay`（→ mobile / staff に同期、お客さん向け画面を作り直し）: 理由の日本語に `second_ear` /
  `fuzzy_keyword` / `amount_only`。
- fixture（`tests/fixtures/store/2026-09-29-*`）: 9/29 の第 2 の耳の結果を、ライブが記録する形で書き起こしに入れた。
  d0f055fb ハンド 1 の真のアクションをオーナーの確認どおりに直した。
- tests: `tests/test_second_ear_live.py`（録音側・記録・CLI・評価の読み直し・規則・読み込み）。

## Expected vs implemented

- 9/29 の 3 セッション（真のアクション 110 行、ハンド 1 を直したもの）: 記録 76 行（69%）→ いまのコードで読み直すと
  **92 行（82%）**（fixture の `reparse_baseline` で固定: d0f055fb 39 → 49、fded6f75 26 → 32、a6ee12e4 11 → 11）。
- この環境（4 コア）の実モデルで、店舗の音声を録音側の経路に通した: 「せんごやく」→ 1500（0.68 秒）、
  「にせんたん」→ 2000（0.25 秒）、雑談「撮れないからね。」→ 使わない（0.14 秒）。読み込みは 3〜5 秒
  （冷えたディスクで 19 秒）。d0f055fb では 237 発話のうち 177 発話（Whisper が読めない会話が多い）を聞き直す。
- 聞き直した行は要確認になる（`second_ear`）。要確認の精度は下がる（28% → 14%）が再現率は上がる（81% → 92%）。

## Test results

- 変更部分: `tests/test_second_ear_live.py` 24、`tests/test_second_ear.py`・`tests/test_tools_second_ear.py`・
  `tests/test_tools_eval_store.py`・`tests/test_store_fixtures.py`・`tests/test_store_2026_09_29.py` 通過。
  shared UI の TS テスト 10 通過、`build_player_web.py --check` 通過。
- 全体: 1950 passed / 5 skipped（`pytest tests/ --ignore=tests/test_vision.py`）、ruff 通過。

## Remaining gaps

- 店舗 PC での確認（更新でモデルを取得できるか・聞き直しの遅れ・CPU）。
- 027e4b15 のリバーの「六千五百」（オーナーが音声を聞く）。
- 額だけの発話がベットでない場合（ポット・持ち点の読み上げ）の扱いは、卓の状態と合わせる推定器（S3）で。
- 第 2 の耳の候補に席番号・ポジションは無い（Whisper が読めた発話は変えない）。

## Related commits

- （この worklog と同じ commit）
