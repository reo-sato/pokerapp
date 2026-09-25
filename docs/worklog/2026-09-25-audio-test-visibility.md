# Worklog: 音声テストの見える化（ADR-0060）

## Date

2026-09-25

## Scope / Task

オーナー「このUIを流用して、音声入力を含めたテストを行います。」— お客さん向けの画面（ADR-0059）を使って、
マイク（faster-whisper）を含むハンドの記録を店舗 PC で試す。その前に、音声の経路で何が起きているかを見えるようにする。

## Goal

- マイクの番号・音量・RDP・プライバシー設定・モデルの取得を本番の前に確かめられる。
- テスト中、発話ごとに「何と聞こえた → 何になった → どう直した」を CLI とスマホで追える。
- 音声の準備に失敗してもハンドロガーは止まらない。

## Changed Files

- `audio/recorder.py` — `Transcript` / `describe_event` / `on_transcript`、書き起こしを INFO で残す（読めない発話も）、
  死活表示に `device_name` / `error`、`asr_ready`。
- `audio/recognizer.py` — `WhisperTranscriber.ready` / `load_error`、ImportError 以外の読み込み失敗も捕まえる。
- `main.py` — `_make_audio_thread(on_transcript=)`、`_print_transcript`、`_report_audio_start`、`run_cli` の読み込み表示と
  モデル失敗時の続行。
- `core/hand_log.py` / `integration/engine.py` — `ActionRecord.raw_text`（rules-aware と unresolved）。
- `docs/contracts/schemas/action.schema.json` — 1.4（`raw_text`）。golden fixtures 13 ファイル（`raw_text` 34 件）。
- `tools/audio_check.py`（新規）— list / level / listen。
- `shared/hand_replay/handReplayModel.ts`（`heardDetails` / `reasonLabels` / ReplayAction の optional 追加）/
  `HandReplay.tsx`（`showHeard`）/ `handReplayModel.test.ts` → mobile / staff へ同期。
- `mobile/src/testMode.ts`（+ test）/ `mobile/src/screens/HandDetailScreen.tsx` / `mobile/src/api/types.ts` /
  `mobile/package.json`（test 対象）→ `api/static/player/` を作り直し。
- tests: `tests/test_audio_check.py`（新規）/ `tests/test_main_audio_optional.py` / `tests/test_reconstruction.py`。
- docs: ADR-0060（新規）/ `docs/troubleshooting.md` / `docs/usage.md` / CLAUDE.md / CHANGELOG / decision-log / 本 worklog。

## Expected Behavior

- `--cli`（音声有効）: 「音声認識モデル（medium）を読み込んでいます…」→ 所要秒数 → 「音声入力: マイク 番号 N（名前）で
  聞き取っています」→ 発話ごとに `[聞き取り] 「…」→ …` と、続けて記録したアクションの行。
- モデルが無い / 取れない: 理由を出して音声なしで続行。マイクを開けない: 理由と `audio_check list` の案内。
- ハンドの記録の各アクションに発話（`raw_text`）。合成 fold には無い。
- スマホ画面は `?test` の端末だけ、各アクションの下に発話と補正の内容・理由。

## Implemented Behavior

- 期待どおり。ヘッドレス Chromium（390×844）で `?test` のハンド詳細を確認: 合成 fold に「（発話なし）/ 声のない
  フォールド（あとで言われた席から補った）」、手番と違う席のコールに「手番と違う席を言った（間の席をフォールドにした）」、
  ベットに対するチェックに「聞き取り チェック → コール / ベットがあるのにチェック → コールにした」、金額なしの
  ベットに「金額が聞き取れず最小額にした」。`?test` 無しでは表示なし。
- この環境には PyAudio / faster-whisper が無いので、実マイク・実モデルでの確認はしていない（fake の PyAudio と
  transcriber でテスト）。

## Mismatches

- 最初は legacy 経路にも `raw_text` を載せる案だったが、「legacy の出力は従来どおり不変」（ActionRecord の規約）に
  合わせて rules-aware と unresolved だけにした（店舗は pokerkit）。

## Fixes

- 上記のとおり。golden は再生成し、差分が `raw_text` の追加だけで、値がそのケースのイベントの文と一致することを
  スクリプトで確認した（34 件）。

## Test Results

- `tests/test_audio_check.py` 16 / `tests/test_main_audio_optional.py` 23 / reconstruction + contracts 127 passed。
- 全体: `pytest tests/ --ignore=tests/test_vision.py` 1322 passed、`ruff check .` clean。
- `mobile/`: `npx tsc --noEmit` OK、`npm test` 33 pass。`python scripts/build_player_web.py --check` OK。

## Remaining Gaps

- 店舗 PC での実マイク: RDP の音声設定（接続元の端末の設定）・プライバシー設定・番号の選び方は手順で案内。
- 発話の傾向（言い方ごとの `needs_review`）が集まったら、語彙と補正を見直す（R 系の数値較正）。
- staff アプリのリプレイには聞き取りを出していない（必要なら `showHeard` を渡すだけ）。

## Related Commits

- （本 worklog と同じコミット）
