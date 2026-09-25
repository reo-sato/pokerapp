# ADR-0060: 音声テストの見える化 — 聞き取った文を CLI・ハンドの記録・画面のテスト表示に出し、マイク確認ツールを用意する

## Status

Accepted（2026-09-25, 実装済。店舗 PC での音声テストは未）

## Date

2026-09-25

## Context

オーナーの次の予定は「お客さん向けの画面（ADR-0059）を流用して、音声入力を含めたテストを行う」。
これまでの店舗の実機テストは RFID とキーボードの読み上げ文だけで、マイク（faster-whisper）の経路は店舗 PC で
一度も通していない。調べると、音声のテストを始めても**何が起きているか見えない**箇所が 4 つあった。

1. **何と聞こえたかが残らない**。書き起こしは DEBUG ログだけで、店舗のランチャ（`--cli --log-file`, INFO）では
   出ない。アクションとして読めなかった発話は完全に消える。ハンドの記録にも発話は残らない（`AudioEvent.raw_text`
   は sidecar の記録を有効にしたときだけ）。誤りがあっても「言い方の問題」か「聞き取りの問題」か「補正の問題」かを
   切り分けられない。
2. **音声の準備状況が見えない**。音声認識モデル（`medium` ≈ 1.5 GB）はセッション設定の入力のあとに読み込まれ、
   初回はダウンロードする。ログはファイルに行くので、コンソールは数分固まったように見える。マイクを開けなかった
   ときもファイルに ERROR が出るだけで、話しても何も起きない。モデルの取得がネットワーク不通などで失敗すると
   （ImportError 以外）、例外がハンドロガーの起動を落とす。
3. **マイクの選び方の手段が弱い**。`audio.device_id` の番号を調べる方法は troubleshooting の 1 行（全デバイスを
   並べるだけ）で、方式（MME / WASAPI …）や 16 kHz で開けるかが分からない。**店舗 PC は RDP で操作**しており、
   RDP は既定で音声を接続元に回すので、PC に挿したマイクがセッションから見えない・無音になることがある。
4. **スマホの画面では補正が見えない**。「要確認」バッジは出るが、何と聞こえて何に直したのかは出ない。

## Decision

### D1. 聞き取った文はすべて報告する（`AudioThread`）

- 文字になった発話ごとに `Transcript`（文・信頼度・アクションとしての読み・発話の長さ・認識にかかった秒数・
  話し始めの時刻・文字になった時刻）を作り、**INFO でログに残し**、`on_transcript` コールバックに渡す。
  **アクションとして読めなかった発話も**渡す（「アクションとして読めず」）。空の書き起こし（雑音）は渡さない。
- コールバックはアクションを queue に積む**前に**呼ぶ（表示で聞き取りがアクションの行より先に出る）。
  コールバックの例外はアクションの処理を止めない。
- 死活表示に開いたマイクの名前（`device_name`）と開けなかった理由（`error`）を載せる。

### D2. CLI に音声の準備状況と聞き取りを直接出す

- 音声が有効なら、モデルの読み込み前に「音声認識モデル（medium）を読み込んでいます…（初回はダウンロードで
  数分かかります）」、あとに所要秒数を出す。
- **読み込みの失敗で落とさない**: `WhisperTranscriber` は ImportError 以外の例外も捕まえて `ready=False` /
  `load_error` にする。CLI は理由を出して**音声なしで続ける**（キーボードの読み上げ文で進行できる）。
- 起動後にマイクを開けたかを出す（「マイク 番号 N（名前）で聞き取っています」/ 開けなかった理由と
  `tools\audio_check.py list` の案内）。
- 聞き取った文を `[聞き取り] 「…」→ raise 600 席3` と出す（記録したアクションの行が続く）。
- ログはファイルのまま（ISSUE-0034 の入力行への割り込み対策は維持）。表示は `print` で main.py に置く
  （規約: CLI 出力は main.py のみ）。

### D3. ハンドの記録に発話を残す（`ActionRecord.raw_text`, action schema 1.4）

- そのアクションになった発話（Whisper の書き起こし / CLI で打った読み上げ文）を optional の `raw_text` に残す
  （additive）。**rules-aware 経路と unresolved レコードだけ**が埋める。合成した fold には無い（言っていない）。
  legacy 経路の出力は変えない（rollback 経路不変の原則）。
- viewer API はハンドの記録をそのまま返すので、画面から読める。

### D4. スマホ画面のテスト表示（URL に `?test`）

- お客さん用の画面の URL に `?test` を付けた端末だけ、ハンドの各アクションの下に**聞き取った文**と、補正したときは
  **その内容**（「聞き取り チェック → コール」）と**理由**（理由コードを短い日本語に。未知のコードはそのまま）を出す。
  合成した fold は「（発話なし）」+「声のないフォールド（あとで言われた席から補った）」。
- 切り替えは端末ごと（サーバーの設定・再起動は不要）。`?test` の無い端末（お客さん）の表示は変えない。
- 共有リプレイ（`shared/hand_replay`, ADR-0044）の `HandReplay` に `showHeard` を足し、mobile の HandDetail が渡す。
  staff アプリは渡さない（表示は従来どおり）。

### D5. マイク確認ツール `tools/audio_check.py`

- `list`: 録音できるデバイス（番号・方式・16 kHz で開けるか・既定・config の番号に印）。無ければ RDP の案内。
- `level`: 入力レベルのバーと「発話」判定（`AudioThread` と同じしきい値）。無音ならプライバシー設定と RDP、
  声が届かなければマイクの位置の案内。
- `listen`: 本番と同じ `AudioThread`（発話の切り出し → Whisper → `parse_action`）で聞き取り、発話ごとに
  「何と聞こえたか → どのアクションになったか」と信頼度・認識の秒数・話し始めからの秒数、最後に件数と平均を出す。
  初回のモデルのダウンロードを本番の前に済ませる用途も兼ねる。
- config は読むだけ（`probe_pcsc` と同じ）。`--device` / `--model` で上書きして比べられる。

## Alternatives Considered

- **聞き取った文をお客さんの画面に常に出す** — 読み上げは卓の公開情報だが、お客さんには雑音。端末ごとの切り替えにした。
- **サーバー側のフラグ（`--viewer-api --show-heard`）で出す** — 起動し直しが要り、見る人を選べない。URL で端末ごとにした。
- **発話の録音（WAV）を残す** — 後から聞き直せるが、容量と店内の会話を録る問題がある。文字だけにした（必要になれば別 ADR）。
- **書き起こしを DEBUG のまま、ログ水準を下げてもらう** — ほかの DEBUG で埋まり、店舗で読めない。INFO の 1 行にした。
- **マイクの一覧を troubleshooting の 1 行のままにする** — 方式と 16 kHz の可否が分からず、RDP で見えない場合を
  切り分けられない。ツールにした。

## Consequences

- Positive: 音声のテストで、発話ごとに「何と聞こえた → 何になった → どう直した」が CLI とスマホの両方で追える。
  マイクの番号・音量・RDP・プライバシー設定・モデルの取得を本番の前に確かめられる。モデルの取得失敗で落ちない。
- Negative / trade-offs: CLI に 1 発話 1 行が増える（入力行に割り込むのは従来のアクション行と同じ）。ハンドの記録が
  1 アクションあたり発話の分だけ大きくなる。RDP の音声設定は接続元の端末ごとの設定で、店舗 PC 側では固定できない。
- Neutral / new constraints: action schema `1.4`（additive）。golden fixtures に `raw_text` が載った（34 件、
  ほかの差分なし）。`mobile/` を変えたので画面を作り直した（ADR-0059 の手順）。

## Validation / Follow-up

- [x] `AudioThread` の報告（アクション・読めず・雑音・コールバック例外）、モデル取得失敗で落ちない
- [x] CLI の表示（読み込み中・マイクの状態・聞き取り）、モデルが無くても `--cli` が最後まで動く
- [x] `raw_text` が golden に載り、合成 fold には無い。schema 1.4 に適合
- [x] テスト表示: 共有モデルの理由の日本語化（TS テスト）と、ヘッドレス Chromium で `?test` の画面を確認
  （`?test` 無しは従来どおり）
- [ ] 店舗 PC: `audio_check list/level/listen`（RDP の音声設定・プライバシー設定を含む）→ 音声を使ったハンド
- [ ] 店舗の発話で `needs_review` が多い言い方を集め、語彙・補正を見直す（R 系の数値較正, CLAUDE.md 残作業 #6）

## Related Files

- `audio/recorder.py`（`Transcript` / `describe_event` / `on_transcript` / 死活表示）/ `audio/recognizer.py`（`ready` / `load_error`）
- `main.py`（`_make_audio_thread(on_transcript=)` / `_print_transcript` / `_report_audio_start`）
- `core/hand_log.py`（`raw_text`）/ `integration/engine.py` / `docs/contracts/schemas/action.schema.json`（1.4）
- `tools/audio_check.py`
- `shared/hand_replay/`（`heardDetails` / `reasonLabels` / `showHeard`）/ `mobile/src/testMode.ts` / `mobile/src/screens/HandDetailScreen.tsx`
- `api/static/player/`（作り直し）

## Related Tests

- `tests/test_audio_check.py` / `tests/test_main_audio_optional.py::TestCliAudioStatus`
- `tests/test_reconstruction.py::test_actions_keep_what_was_heard` + golden fixtures
- `shared/hand_replay/handReplayModel.test.ts` / `mobile/src/testMode.test.ts`

## Related Commits

- （本 ADR と同じコミット）

## Supersedes / Superseded by

- Supersedes: —
- Superseded by: —
