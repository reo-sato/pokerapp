# ADR-0069: 第 2 の耳 — 日本語専用の音声認識（ReazonSpeech ONNX）を Whisper と並べて聞く

## Status

Accepted（2026-10-13 に記録。2026-09-29 に測定とライブの聞き直し、2026-10-07 に全発話の記録と短い語の分類器を実装済）

## Date

2026-10-13（決定は 2026-09-29・2026-10-07）

## Context

ライブの聞き取りは faster-whisper medium（ADR-0061）。店舗のディーラーは席番号もベット・レイズの語も言わず、短く崩して
早く言う（「コル」「こぅ」= コール、「これだ」= フォールド、「にせんたん」= 2000）。Whisper は:

- 弱い・短い音を、よくある日本語やプロンプトの繰り返しに置き換える（幻聴「ご視聴ありがとうございました」）。店舗 7 セッション
  265 発話で幻聴 73・読めない文 50（`docs/worklog/2026-09-29-asr-root-fix-exploration.md`）。そのうち 11〜14 件は、別の
  2 つのモデルがそろって「六百」「八百」「コール」などと聞いた。
- 額を仮名で崩す（「せんごやく」= 1500、「のっぴょく」= 600）。表記揺れを辞書に 1 つずつ足す直し方は限界だった。
- 1 発話 2.8〜2.9 秒（店舗 PC, 8 スレッド）で、候補を 1 つずつ Whisper で採点するのは重い（30 秒の窓で 1 候補 0.8〜0.9 秒）。

オーナー（09-29）:「表記揺れのような音声入力間違いの根本解決の方法を模索してください。Fable 5.1、Sonnet 5.5 の案も聞いて
みてください」。検討（3 者の案と実験）の結論は「閉じた語彙の仕事に開いた語彙の 1 位の文字列を使っているのが原因。観測を
『決まった候補ごとの確からしさ』にし、決めるのは推定器（ADR-0056）」。その観測層の第一歩として、軽い日本語専用の
モデルを Whisper と並べることにした（オーナー「進めてください」→ 評価のあと「次の一歩はその方針で」）。

## Decision

### D1. モデルと入れ方

- **ReazonSpeech k2 v2**（日本語専用・文字単位の RNN-T。k2-fsa / sherpa-onnx が配る ONNX 版, Apache-2.0）を `audio/second_ear.py`
  で onnxruntime から動かす。1 発話 0.1〜0.3 秒（CPU, int8）。
- 発話ごとに (1) 自由に聞いた文（貪欲な探索）とその確からしさ、(2) 決まった候補（アクションの語・100〜50000 の額・
  レイズ / ベット + 額・額 + コール・2 つのアクション）ごとの log P(候補 | 音声)（前向きアルゴリズム・接頭木でまとめて計算）
  を出す。文字列ではなく音にどの候補が合うかを比べるので、表記揺れに左右されない。
- 入れ方: extra `.[ear]`（`kaldi-native-fbank` / `onnxruntime`）。インストーラは本体とは別の手順で入れ、**失敗しても止めない**。
  モデル（GitHub の配布物 713 MB を流しながら要る 4 ファイル約 170 MB だけ取り出す）は導入・更新のときに `models/` へ取得
  （`installer/install.ps1:Invoke-EarModel`、`-SkipModel` で飛ばす）。`models/` は更新で消さず、`.gitignore`。
- **非致命**: 部品・モデルが無い / 壊れている / 聞き直しが失敗しても、Whisper だけで続ける（`load_live` が読み込み時に一度
  聞いてみる）。config `audio.second_ear` = `{"enabled": true, "threads": 4}`（項目が無い config でも使う。false で使わない）。

### D2. ライブで使うのは Whisper が読めなかった発話の救い出しだけ（2026-09-29）

- Whisper がアクションとして読めなかった発話（空・幻聴・読めない文。確認型の発話と VAD の「声ではない音」は除く）だけを
  聞き直し、第 2 の耳が**自由に聞いた文そのもの**が、いちばん確からしい候補と同じアクションに読めるときだけ、その候補を使う
  （`agreed_candidate` / `rescue_events`）。雑談は使わない。Whisper が読めた発話のアクションは変えない（第 2 の耳には席番号・
  ポジションの候補が無い）。
- 聞き直した行は**要確認**（parse flag `second_ear`, reconstruction_event 0.11、聞き取りの自信 0.5）。CLI に
  「第 2 の耳「六百」→ bet/raise 600」、スマホの `?test` に理由「Whisper が読めず、第 2 の耳で読んだ」。
- その後の広げ方（いずれもライブの読みを変える範囲は「Whisper の読みが欠けた所」に限る）:
  - 09-30: ベット・レイズに額が無い・100 未満なら第 2 の耳の額を入れる（`fill_amounts`）。Whisper の自信が 0.15 未満で第 2 の
    耳がアクションを何も聞いていなければ捨てる。役の名前も候補に。
  - 10-01: 額を読んだ発話も聞き直し、額ごとの点数を `amount_scores`（reconstruction_event 0.14）に残す。engine はいま使える
    額に絞って選ぶ（`legal_amount`）。
  - 10-02: 「千」「万」で終わる額の下の桁を、第 2 の耳の額と自由に聞いた文が合うときだけ埋める（`fill_lower_digits`）。

### D3. 全発話にかけて記録する。ライブの読みは変えない（2026-10-07）

- Fable 5.1 の報告（10-07）: 全発話に第 2 の耳をかけると、額は Whisper 48% / 第 2 の耳の 1 位 95%、短く崩したコールは第 2 の耳には
  「これ」「る」「コル」と聞こえる（「これ」108/115・「る」62/64 がコール）。オーナー承諾で「進め方 1」として実装。
- Whisper にかけた発話はすべて（VAD で落とした声ではない音も、記録だけ）第 2 の耳で聞き、書き起こしの記録
  （`transcripts.jsonl`）に `ear`（自由に聞いた文・候補・額の点数・短い語 `SHORT_WORDS` 42 語ごとの確からしさ）を残す。
- **ライブで使うのは D2 の発話だけ**（記録の `ear_wanted`）。読み直し・再生（`integration/world_replay.py`）・推定器も、
  `ear_wanted` が偽の耳は無いものとする（`second_ear.used_ear`）。1 発話あたり平均 +0.14 秒、記録は 1 セッション約 1 MB。

### D4. 短い語の分類器は推定器だけが使う（2026-10-07）

- `audio/short_words.py`（読むだけ・numpy のみ）: 第 2 の耳の短い語・額・候補の点数から、その発話が「雑談 / コール /
  チェック / フォールド / 額」のどれかの確率を出す（多項ロジスティック回帰。重み `audio/short_word_model.json` は開発データの
  真のアクションから `tools/short_words.py train` で作る。Whisper の読みは使わない = 推定器が Whisper の読みと二重に数えない）。
- 推定器（`tools/estimate.py:_short_word_options`、`integration/estimator.py` の `short_words`）が、分類器の読みを発話の読みの
  選択肢に足す。分類器の読みが既定の読みより高くなるのは log で 1 まで（オーナーの聞き取り = データ C のラベルで学び直した
  あと）。どの読みかはハンドの筋で決める。**ライブの engine・読み取りは分類器を使わない**。

## Alternatives Considered

- **Whisper を ReazonSpeech に置き換える** — 額は強い（店舗の 24 件で 24/24）が、ポーカーの語が崩れる（コル・これど・相談・
  ザップ）。第 2 の耳だけの再生は真のアクションとの一致 62%（いまの読み取り 73%）。置き換えず並べる。
- **SenseVoice small（多言語 CTC）** — 速い（0.09 秒）が額の桁落ちが多い（1400 → 140、2千 → 二0）。不採用。
- **Whisper の強制デコードで候補を採点する（Fable 5.1 の 1 周目の本命）** — 30 秒の窓で 1 候補 0.8〜0.9 秒、10 候補で 12.6 秒。
  短い窓（2.5〜4 秒）でも候補 10 個で 1.6〜1.9 秒で、精度は未確認。ライブには重い。事後の上位数個の採点に回す案として残す。
- **プロンプトの撤去・短い窓・微調整** — 撤去と短い窓は Whisper をライブに残す場合の高速化・幻聴対策で、精度は店舗 PC で
  未確認。微調整（意図分類器を含む）はラベルが数百発話以上たまってから。D4 の分類器はその軽い第一歩。
- **救い出した発話を無条件にライブのアクションにする** — ポットの読み上げ・雑談が誤アクションになり、要確認を食う
  （Fable 5.1・Sonnet 5.5 の 2 周目の批評）。自由に聞いた文と候補が一致したときだけ・要確認で入れ、ほかは観測として推定器へ。
- **全発話の第 2 の耳でライブの読みも変える** — 短い語は音だけでは決まらない（「コル」と「これ」、「これど」と「フォールド」）。
  一律の事前確率で一致を上げると声の無い発話への誤反応が 0 → 7 に増えた。卓の状態と合わせる推定器でだけ使う（D3・D4）。
- **声の高さ・大きさで宣言と雑談を見分ける（10-03 の調べ）** — AUC 0.69 で事前の基準 0.70 に届かず、開発データの正誤も変えない。
  使わない（`tools/voice_style.py` は道具として残す）。

## Consequences

- Positive:
  - 店舗 9/29 の 3 セッション（真のアクション 110 行）で、記録 69% → 82%（ライブの救い出し、回帰テストに固定）。評価の再生では
    73% → 80%。
  - 全発話の観測（候補・額・短い語の点数）がたまり、推定器と分類器の入力になった（開発データの全部正しいハンド 71 → 75/99,
    10-07。標本内）。
- Negative / trade-offs:
  - 導入に約 170 MB のモデルと onnxruntime が要る。入らない店舗 PC では救い出しが無く、記録は Whisper だけの精度に戻る
    （止まりはしない）。
  - 1 発話あたり +0.14 秒（全発話）。店舗 PC の Whisper（1 発話 約 3 秒）に比べれば小さいが、遅れの上乗せはある。
  - 救い出した行・下の桁を埋めた行はすべて要確認なので、要確認の数が増える。
  - 分類器の重みは開発データ（同じディーラー 1 人）で学んだ標本内の値。
- Neutral / new constraints:
  - 決定性: 推定器・再生は記録した `ear` を読むだけで、第 2 の耳を実行し直さない（ISSUE-0010 の記録境界と同じ）。
  - 第 2 の耳の候補には席番号・ポジションが無い。席を言った発話は Whisper の読みのまま。

## Validation / Follow-up

- [x] `tests/test_second_ear.py`（前向きアルゴリズムを全アラインメントの数え上げと照合・接頭木・取得）/ `tests/test_second_ear_live.py` /
  `tests/test_tools_second_ear.py` / `tests/test_short_words.py` / 店舗の開発データ（`tests/fixtures/store/`）。
- [ ] 評価のセッション（ADR-0056 追記 4〜6）で、救い出した行の誤受理（ポットの読み上げ・雑談）の率を数える。
- [ ] onnxruntime のスレッド数による結果の違い（決定性）を店舗 PC で確かめる（検討で挙がった点。未確認）。
- [ ] 分類器をラベルが増えたら学び直す（`tools/short_words.py train`）。

## Related Files

- `audio/second_ear.py` / `audio/short_words.py` / `audio/short_word_model.json`
- `audio/recorder.py`（`wants_ear` / `apply_ear` / `ear_wanted`）/ `output/transcript_log.py`
- `tools/second_ear.py` / `tools/short_words.py` / `tools/fixture_ears.py` / `tools/estimate.py` / `integration/estimator.py`
- `pyproject.toml`（extra `ear`）/ `installer/install.ps1`（`.[ear]`・`Invoke-EarModel`）/ `config_default.json`（`audio.second_ear`）

## Related Tests

- `tests/test_second_ear.py` / `tests/test_second_ear_live.py` / `tests/test_tools_second_ear.py` / `tests/test_short_words.py`
- `tests/test_store_2026_09_29.py` / `tests/test_store_fixtures.py`

## Related Commits

- 2026-09-29 の測定とライブの聞き直し: このリポジトリの履歴が浅く引けない（`docs/worklog/2026-09-29-second-ear-evaluation.md` /
  `2026-09-29-second-ear-live.md`）
- `08dda90` — 数の下の桁（`fill_lower_digits`）
- `29ff2fd` — 第 2 の耳を全発話にかけて記録する（ライブの読みは変えない）
- `e1a1e8b` — 開発データの全発話に第 2 の耳の結果を足す
- `ba4cec9` — 推定器: 全発話の第 2 の耳と短い語の分類器
- `73ab8bb` — 分類器の学習ラベルをオーナーの聞き取りで直す

## Supersedes / Superseded by

- Supersedes: —（ADR-0061 の Whisper medium はそのまま。第 2 の耳は並べるだけ）
- Superseded by: —
- 関連: ADR-0056（事後推定 = 観測を使う側）/ ADR-0060（聞き取りの見える化）/ ADR-0061 / ADR-0068
