# 2026-09-27 Whisper の CPU スレッド数を測って決める

## Goal

オーナー「PC の性能ギリギリかなぁ」（音声の採点ツールを店舗 PC で動かす話のあと）。店舗の記録で聞き取りの
速さを確かめ、使っていない CPU を使えるようにする。既定の動きは変えず、店舗 PC で測ってから決める。

## 店舗の記録（2 つの zip、声として聞き取りに回した 224 発話）

- 発話の長さ: 中央値 1.3 秒・9 割 2.5 秒・最大 5.1 秒。
- 1 発話の聞き取り（推論）: 中央値 2.83 秒・9 割 4.06 秒・最大 9.22 秒（Whisper は発話が短くても 30 秒の窓を
  処理する）。セッションごとの中央値はどれも 2.7〜2.9 秒。
- 話し始めから文字になるまで: 中央値 5.0 秒・9 割 11.2 秒・最大 25.2 秒 = 続けて話されると待ちがたまる。
- faster-whisper の `cpu_threads` は既定 0 = CTranslate2 の既定の 4 スレッド（faster-whisper 1.2.1 の docstring
  「4 by default」）。店舗 PC（GEEKOM A8）は 8 コア 16 スレッド。

## Changed files

- `audio/recognizer.py`: `WhisperTranscriber(cpu_threads=0)` → `WhisperModel(cpu_threads=...)`。
- `audio/recorder.py` / `main.py` / `tools/audio_check.py`（listen）: config `audio.cpu_threads` を渡す。
- `config_default.json`: `audio.cpu_threads: 0` + 説明。
- `tools/audio_check.py bench`: いちばん新しい（`--session`）セッションの保存した発話から、声だった発話を全体に
  散らして `--count` 個（既定 10）。設定（スレッド数 × ビーム幅）ごとにモデルを読み込み、1 回目は数えずに同じ
  発話を聞き取って時間を測る（ライブと同じ `recognize` = VAD + Whisper）。いまの設定より 1 割以上速く、
  書き起こしが同じ設定を選び、`set_config.py` のコマンドを出す。
- `tools/rescore_audio.py`: `--threads`（既定 = 論理コアの半分、4 以上）。

## Tests

- `tests/test_audio_check.py`（+7）: 発話の選び方（新しいセッション・声でない行と音声の無い行を飛ばす・散らす）/
  WAV を PCM16 16 kHz モノラルに / 速くて書き起こしが同じ設定を勧める（ビームを下げて書き起こしが変わる設定は
  勧めない）/ 速くならなければ勧めない / コマンドライン / 保存した音声が無い / スレッド数が faster-whisper に渡る。
- `tests/test_main_audio_optional.py`（+1）: config の `audio.cpu_threads` が渡る（既定は 0）。
- 重みが乱数の小さな Whisper と店舗の zip の WAV で `bench` が最後まで動く（小さいモデルではスレッド 1 が
  いちばん速い = 勧める設定は測った結果しだい）。
- 全体 1840 passed / 6 skipped、ruff clean。

## 店舗 PC での計測（オーナー実行, 2026-09-27）

`audio_check bench`（セッション b64bf0bf の発話 10 個・medium・論理コア 16）:

| 設定 | 1 発話 中央値 | 最大 | 書き起こし |
|------|---------------|------|------------|
| スレッド 4・ビーム 5（いまの既定） | 3.70 秒 | 4.92 秒 | — |
| スレッド 8・ビーム 5 | 2.96 秒（80%） | 3.65 秒 | 10/10 同じ |

→ 店舗 PC は `audio.cpu_threads` 8 にする（`set_config.py`）。4 スレッドの中央値が記録の 2.83 秒より遅いのは
発話の違い（別のセッション）か、計測中のほかの負荷と思われる。

## Remaining gaps

- 16 スレッド（SMT）とビーム幅（5 / 3 / 1）はまだ測っていない（`bench --threads 8,16 --beam 5,3,1`）。
- 8 スレッドでも 1 発話 約 3 秒 = 続けて 3 つ言われると 3 つ目は 9 秒ほど遅れる。ビーム幅で足りなければ、
  待っている発話をまとめて処理する（バッチ）のが次の手。
- 速くなっても待ちが残るなら、次の手はビーム幅（`--beam 5,3` で書き起こしが変わらないか確かめる）。
