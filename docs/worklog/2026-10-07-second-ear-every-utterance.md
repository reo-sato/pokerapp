# Worklog: ライブでも第 2 の耳を全発話にかけて記録する（短い語の聞き間違いの進め方 1）

## Date

2026-10-07

## Scope / Task

`docs/worklog/2026-10-07-short-word-plan.md` の進め方 1（オーナー承諾）: ライブでも第 2 の耳を全発話にかけて記録する。
**ライブの動きは変えない**。記録した結果を、進め方 2（短い語の分類器・推定器への組み込み・オーナー案の逆算）で使う。

## Expected vs Implemented

- 期待: 全発話の第 2 の耳の結果（自由に聞いた文・候補・額の点数）と、短い語ごとの確からしさが記録に残る。ライブの読み・
  読み直し・再生・推定器の結果は今までと同じ。
- 実装:
  - `audio/second_ear.py`: `SHORT_WORDS`（42 語。コールの崩れ「コル」「こる」「コー」「こう」「こぅ」「る」「これ」、
    フォールドの崩れ「これだ」「あれだ」「俺だ」、ストリートの言い方「ターン / ターンです / ターンカード / リバー /
    リバーです / リバーカード / ラストカード / フロップ」、残りの人数、雑談・決まり文句の語）。読み取りの候補とは別の木で
    採点するので、候補の上位・額の表は変わらない。`EarResult.words`（語 → 確からしさ）・`frames`（発話の長さ）を
    `to_dict()` に足した。「こる～」はモデルの文字に「～」が無いので入れていない（「こる」「こぅ」で代わる）。
  - `audio/recorder.py`: Whisper にかけた発話はすべて第 2 の耳で聞く（声ではない音 = VAD で落とした音も、記録だけ）。
    ライブで使うのは今までどおり Whisper が読めなかった発話と額を読んだ発話（`wants_ear` / `wants_amount_scores`）だけで、
    それ以外は `apply_ear` に耳を渡さない（= 今までと同じ）。`Transcript.ear_wanted` = ライブが使った発話か。
  - `output/transcript_log.py`: `ear_wanted` を書く（耳のある行だけ）。
  - 読む側: `second_ear.used_ear(row)` = `ear_wanted` が偽の耳は無いものとする（無い古い記録は、耳があれば使った）。
    読み直し（`tools/eval_store.py` の 3 か所）・再生（`integration/world_replay.read_utterance`）・推定器
    （`tools/estimate.utterance_options`）・声の研究（`tools/voice_style.py`）をこれに替えた。タイムラインの表示
    （`eval_store timeline`）と `audio_check listen` の表示は、全発話の耳を見せる（診断用）。
  - 開発データの書き出し（`_FIXTURE_TRANSCRIPT_KEYS`）に `ear_wanted` を足した。

## 時間（1 発話あたり）

- この開発環境（4 コア）で 782c457d の 60 発話: 今までの第 2 の耳 平均 0.274 秒 → 短い語つき 0.342 秒（+0.068 秒）。
  候補の上位・額の表・自由に聞いた文はすべて同じ（確かめた）。
- 店舗 PC の記録（782c457d）: 第 2 の耳 平均 0.18 秒（聞いた 209 発話）、Whisper 平均 2.96 秒。今まで耳をかけていなかった
  発話（約 55%）にも 0.2 秒ほどかかるので、1 発話あたり平均 +0.14 秒ほど（Whisper の 5% ほど）。承諾のときに伝えた
  「0.05〜0.1 秒」より少し多い。

## 記録の大きさ

- 耳のある行は 1 行 約 1 KB → 短い語つきで約 2 KB。全発話（1 セッション 460 行ほど）で書き起こしの記録は 1 MB ほど。

## Changed Files

- `audio/second_ear.py` — `SHORT_WORDS`・`EarResult.words` / `frames`・`SecondEar(words=)`・`used_ear`。
- `audio/recorder.py` — 全発話を聞く・`Transcript.ear_wanted`。
- `output/transcript_log.py` — `ear_wanted`。
- `integration/world_replay.py`・`tools/eval_store.py`・`tools/estimate.py`・`tools/voice_style.py` — `used_ear`。
- `tests/test_second_ear.py`・`tests/test_second_ear_live.py`・`tests/test_estimate.py`・`tests/test_garbled_amount.py`。
- `CHANGELOG.md`・本作業ログ・`docs/estimator.md` §5（内容の指紋）。
- 進め方 2 の準備: `tools/fixture_ears.py`・`tests/test_fixture_ears.py`・`tests/fixtures/store/*/transcripts.jsonl`。

## Test Results

- 第 2 の耳まわり（`test_second_ear*`・`test_garbled_amount`・`test_lower_digits`・`test_amount_space`・
  `test_tools_second_ear`・`test_read_corpus`）269 passed、推定器（`test_estimate`）・再生の読み 82 passed。
- 全体のテスト 3066 passed・5 skipped（`pytest tests/ -n 4 --ignore=tests/test_vision.py`）、`ruff check .` 指摘なし。
- 物差し（`TZ=Asia/Tokyo python tools/bench_hands.py --twice --search-check --workers 3`、内容の指紋 8cd7f4cd3d02）:
  店舗 99 ハンドで読み直し 63 → 推定 71（95% 区間 62%〜80%）、良くなった 9・悪くなった 1（e82f5005#15、要確認つき）、
  誤り 28 = 候補に無い 17・点で負けた 11。台本 27/30 → 29/30。2 回とも全ハンド同じ、探索 2 倍で 1 番が変わるハンド 0。
  前の物差し（2790cd2）との違いは 782c457d#1 だけで、これはその後にオーナーの指摘で真のアクションを直したため
  （8a0d58d。読み直しも誤りになって「悪くなった」から外れ、推定の正解はまだ候補に無い）。ハンドごとの一覧はほかに
  1 つも変わらない = 今回の変更は推定に影響しない。

## 進め方 2 の準備: 開発データの全発話に第 2 の耳を足した

- `tools/fixture_ears.py`: 開発データ（`tests/fixtures/store/*/transcripts.jsonl`）の各行に、保存した発話の音声
  （店舗の logs、ファイル名 = 話し始めの時刻のミリ秒）から第 2 の耳の結果を足す。耳の無い行は記録だけの耳
  （`ear_wanted` = 偽、`ear.offline` = 真）、ライブの耳の行は短い語（`words`・`frames`）だけを足す（候補・額の表は
  ライブのまま）。もう足した行は聞かない（何度流してもよい）。店舗の音声はリポジトリに入れない。
- 開発データ 16 セッション 2,738 発話をすべて聞いた（足した 1,278・短い語を足した 1,460・音声なし 0・失敗 0、この
  開発環境で約 18 分）。記録だけの耳は使わないので、読み直し・再生・物差しは変わらない（開発データのテストはそのまま
  通る）。開発データの大きさ 3.6 MB → 7.9 MB。

## Remaining Gaps

- Whisper が空の文を返し、第 2 の耳でも読めなかった発話は、今までどおり記録しない（開発データ 2738 行に 1 行も無い）。
- 進め方 2 の分類器と推定器への組み込みはこのあと（別の作業ログ）。

## Related Commits

- 本作業ログと同じコミット。
