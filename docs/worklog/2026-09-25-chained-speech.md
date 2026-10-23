# Worklog: 音声の実測への対応 — 続けて言ったアクション・発話を捨てない・短い言葉（ADR-0061 / ISSUE-0037）

## Date

2026-09-25

## Scope / Task

店舗 PC での `tools/audio_check.py`（ADR-0060）の実測を受けた修正。途中でオーナーから 2 点:
「実運用ではシート番号は発話されない予定です」「遅れが積み重なっても発話のタイミングが記録されていて、最終的に
全体を解釈できれば問題ないのでは（プレー中に発話やアクションが止まる時間はある）」。

## Goal

席番号を言わない運用で、ディーラーが言ったアクションが言った順に 1 つも欠けずに記録される。遅れは受け入れ、
発話は捨てず、打った入力が音声を追い越さない。

## 店舗の実測（ワイヤレスマイク 番号 1、MME）

- `level`: 録音レベル調整前は声 250〜850（しきい値 300 すれすれ）→ 調整後 1000〜6000、雑音 55〜150。
- `listen`（medium）: 書き起こしは正確。認識 3.0〜3.9 秒 / 発話、話し始めからの遅れ 4.3 → 9.5 秒。
- `listen`（medium, `OMP_NUM_THREADS=8`）: 認識 2.6〜3.1 秒（平均 2.9）。遅れは積み上がる。
- `listen`（small, 8 スレッド）: 認識 1.0〜1.1 秒だが「ベット 1200」→「ベット100」、「ウィナー」→「ミーナー」。
- 続けて言うと 1 発話: 「シート4 コール、シート1 フォールド」「シート2 ベッド1200、シート5 レイズ2500」
  「フォールド、シート3、ウィナー」「ハンド開始、シート3 レイズ 600」。「チェック」は 2 回とも出なかった。

## Changed Files

- `audio/recognizer.py` — `_keyword_matches` / `_distinct_keywords` / `_split_points` / `parse_actions`
  （9 個以上は分けず `too_many_actions`）。
- `core/constants.py` — 「ベッド」= bet、「チェックレイズ」= raise。
- `audio/recorder.py` — 推論待ちの上限を撤廃（20 件超で WARN）、`backlog()`、`min_speech_sec`（既定 0.15）、
  `on_dropped`、`Transcript.events`（複数）、`describe_events`。
- `main.py` — `_wait_for_backlog`（打った行・`q` の前に待つ）、打った行を `parse_actions` で分割、
  `audio.min_speech_sec` を渡す、`_print_transcript` を複数対応。
- `tools/audio_check.py` — 席番号なしの例、1 発話の複数アクション、短すぎて捨てた音の行、`--min-speech`、
  終了時に残りを表示。
- `tools/play_hand_text.py` — `parse_actions`。
- `config_default.json` — `audio.min_speech_sec`。
- `shared/hand_replay/handReplayModel.ts` — `too_many_actions` の表示 → mobile / staff に同期、画面を作り直し。
- tests: `tests/test_multi_action_utterance.py`（新規）/ `tests/test_reconstruction_hardening.py` /
  `tests/test_audio_check.py` / `tests/test_main_audio_optional.py`。
- docs: ADR-0061 / ISSUE-0037（新規）/ ADR-0047 追記 / `docs/usage.md`（席番号を言わない運用）/
  `docs/troubleshooting.md`（遅れ）/ CLAUDE.md / CHANGELOG / decision-log / 本 worklog。

## Expected Behavior

- 店舗で 1 発話になった 4 例が、それぞれ 2 アクションに分かれる。「フォールド、フォールド、コール」は 3 つ。
- 「ベッド 1200」は bet 1200。「チェックレイズ 1200」は raise 1 つ。
- 0.19 秒の有音（「チェック」相当）は認識に回る。0.13 秒は捨てて `listen` に報告。
- 推論待ちが何件たまっても捨てない。打った `n` / `w` は認識待ちが無くなってから積まれる。

## Implemented Behavior

- 期待どおり（テストで固定）。実マイクでの再確認は店舗で行う。

## Mismatches

- 「チェックレイズ」は分割すると check + raise の 2 アクションになる（手番がずれる）→ 1 語の raise として語彙に追加。
- 同じアクションの繰り返し（フォールド × 9）は従来の flag（異なるアクションのときだけ）が付かない →
  `too_many_actions` を新設し、画面の理由表示にも足した。

## Test Results

- `tests/test_multi_action_utterance.py` 29 / 音声まわり一式 139 passed。
- 全体: `pytest tests/ --ignore=tests/test_vision.py` 1353 passed、`ruff check .` clean。
- `mobile/`: `npx tsc --noEmit` OK、`npm test` 33 pass、`scripts/build_player_web.py --check` OK。

## Remaining Gaps

- 店舗での再確認（「チェック」・続けて言った発話・遅れがたまっても全部出る・テスト表示でアクターが手番どおりか）。
- 短い言葉の信頼度（0.55 前後）で「要確認」が多い → 実データで較正（ADR-0033）。
- 言い忘れたアクション（席番号なし）を補う仕組みは事後の解釈（ADR-0056）の範囲。
- 速度の調整（beam 幅・スレッド）は必要になったら。

## Related Commits

- （本 worklog と同じコミット）

## 追補（2 回目の実測）

`listen` の再実施: 「チェック」「ベッド1200」は拾えた。「フォールド、フォールド」が「フォールド、ホールド」と
書き起こされて 1 つに → 「ホールド」= fold を追加。「チェックレイズ 2000」が「チェック、レイズ 2千」と区切られて
2 アクションに → 運用で「レイズ 2000」とだけ言う（ISSUE-0037 追記・ADR-0061 追記・usage）。冒頭に雑音の幻聴
「ご視聴ありがとうございました」（アクションの語なし = 影響なし）。認識 平均 3.0 秒、9 件とも最後まで処理。

