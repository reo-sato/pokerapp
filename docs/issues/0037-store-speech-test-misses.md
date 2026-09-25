# Issue 0037: 店舗の音声テストで分かった取りこぼし（続けて言ったアクション・「ベッド」・短い言葉・遅れで捨てる発話）

## Date

2026-09-25

## Status

Fixed（ADR-0061。店舗での再確認は未）

## Severity / Priority

- Severity: High（席番号を言わない運用では、1 アクションの欠け・誤りで以降のアクターがすべてずれる）
- Priority: P1

## Area

audio（`audio/recognizer.py` / `audio/recorder.py`）/ CLI

## Expected Behavior

ディーラーが言ったアクションが、言った順に、1 つも欠けずに記録される（席番号は言わない）。

## Actual Behavior

店舗 PC（ワイヤレスマイク、medium）の `tools/audio_check.py listen`:

```
「シート4 コール、シート1 フォールド」→ call 席4 （multi_action_keywords）
「シート2 ベッド1200、シート5 レイズ2500」→ raise 1200 席2
「フォールド、シート3、ウィナー」→ fold 席3 （multi_action_keywords）
「シート2 ベッド1200」→ アクションとして読めず
認識 平均 2.9 秒 / 話し始めから 4.3 → 9.5 秒（積み上がる）
```

small: 認識 約 1 秒だが「ベット 1200」→「ベット100」、「ウィナー」→「ミーナー」。
2 回とも「チェック」が出てこなかった。

## Reproduction

1. `python tools/audio_check.py listen`（medium）で、2 つのアクションを間を空けずに続けて言う。
2. 「ベット 1200」と言う。
3. 「チェック」とだけ言う。

## Root Cause

1. `parse_action` は 1 発話から先頭の 1 アクションだけを採る（ADR-0047 V1）。
2. 語彙に「ベッド」が無い。読めないベットの後ろのレイズが、発話の最初の席・金額と組み合わさった。
3. 認識に回す有音の下限 0.3 秒が、単独の「チェック」（有音 0.2 秒前後）より長い。
4. 推論待ちの上限 8 件で古い発話を捨てる。

## Fix

ADR-0061: `parse_actions` で続けて言ったアクションを分ける（「チェックレイズ」は 1 つ）/「ベッド」= bet /
有音の下限 0.15 秒（`audio.min_speech_sec`、捨てた音は `listen` に表示）/ キューに上限を設けない /
打った入力は認識待ちの発話を追い越さない。

## Regression Test

- `tests/test_multi_action_utterance.py`（店舗の 4 例と席番号なしの連続・単独のまま残す語・繰り返し・
  AudioThread の分割・短い言葉・捨てた音の報告・打鍵の待ち・打った行の分割）
- `tests/test_reconstruction_hardening.py::TestRecorderT4::test_inference_queue_keeps_every_utterance_in_order`

## Related

- ADR-0061 / ADR-0060（`audio_check`）/ ADR-0056（事後の解釈）/ ADR-0047（V1 の複数アクション）
- `docs/worklog/2026-09-25-chained-speech.md`

## 追加の実測（2 回目, ADR-0061 の反映後）

```
「ご視聴ありがとうございました。」→ アクションとして読めず（信頼度 0.17）   ← 雑音への Whisper の定番の幻聴
「チェック」→ check                                                     ← 捨てずに拾えた
「ベッド1200」→ bet 1200                                                ← 読めた
「フォールド、ホールド」→ fold                                           ← 2 つ目の「フォールド」が「ホールド」
「チェック、レイズ 2千」→ check / raise 2000                             ← 「チェックレイズ」に「、」が入った
認識 平均 3.0 秒（遅れはたまるが、9 件とも最後まで処理）
```

- 「ホールド」= fold を語彙に追加（ポーカーの読み上げに「ホールド」は無い。「ホールデム」とは重ならない）。
- 「チェックレイズ」は Whisper が「チェック、レイズ」と区切ることがあり、2 人のアクション（チェック → レイズ）と
  区別できない。**チェックレイズは「レイズ 2000」とだけ言う**運用にする（語彙の「チェックレイズ」= raise は、
  区切られずに書き起こされたときのために残す）。
- 幻聴（「ご視聴ありがとうございました」）はアクションの語を含まないので記録に影響しない。

