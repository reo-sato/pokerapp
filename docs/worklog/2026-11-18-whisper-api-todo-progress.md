# Worklog: Whisper の外部 API の TODO を進める（中継・イメージ・後継の比べ）

## Date

2026-11-18

## Scope / Task

オーナー「進められるだけ進めてください」を受け、`docs/whisper-api-todo.md`（ADR-0072）のうち、コードと開発の環境で進められる
項目を進めた。

## Goal

- store-data の音声の 90 日の削除の道具（別のコミット e9c8aa6）。
- 評価の道具に書き起こしの方式ごとの数と費用の目安（C）。
- whisper-1 の提供終了と後継を調べ、同じ物差しで測る（D）。
- クラウドの中継とクラウドのイメージ（E, ADR-0067 2e の一部）。

## Changed Files

- `core/remote_asr.py` — `CloudTranscriber`（クラウドの中継）・`USD_PER_MIN`（モデルごとの費用の目安）。
- `main.py` — `audio.transcriber=cloud`（クラウドにつないでいれば中継、無ければ店舗 PC の Whisper）。
- `cloud/app.py`・`cloud/__main__.py` — `POST /api/cloud/v1/asr/transcriptions`（端末トークン・店ごとの 1 日の上限・保存しない）。
- `tools/eval_store.py` — 「書き起こしの方式」の行。
- `tools/cloud_asr.py` — 比べる候補に `openai-transcribe`（gpt-transcribe）。
- `cloud/Dockerfile`・`.dockerignore`・`cloud/cloudbuild.yaml` — クラウドのイメージと Cloud Run へのデプロイ。
- テスト: `tests/test_cloud_asr_relay.py`・`tests/test_cloud_image.py`・`tests/test_remote_asr.py`・`tests/test_cloud_app.py`・
  `tests/test_tools_eval_store.py`。
- 文書: `docs/whisper-api-todo.md`・ADR-0072 追記 3・`docs/cloud-setup.md`（2・4）・`docs/plan-board.md`・CHANGELOG・CLAUDE.md。

## Expected Behavior

- 中継: 店舗 PC に鍵を置かずに API で書き起こせる。失敗・上限はテスト中の形と同じく音声を残して送り直す。
- イメージ: クラウドの部品だけを持ち、店のデータ・秘密を入れない。手元で起きる。

## Implemented Behavior

- 期待どおり。イメージは手元で作り（この環境では Docker Hub の回数制限と通信の証明書のため、基のイメージを
  `mirror.gcr.io` にし、証明書を build の秘密で渡した手元用の写しで作った）、起動して次を確かめた。
  - health は ok。
  - 中継の受け口は、端末トークンが無いと 401、鍵が無いと 503。
  - お客さんの画面は 200。
  - 動くユーザーは root ではない（uid 10001）。

## 後継の比べ（いまのコードの推定器, 全部正しいハンドの数）

| 日 | 店舗 PC の Whisper | whisper-1 | gpt-transcribe |
|---|---|---|---|
| 10/29 | 24/120 | 25/120（+2 / −1） | 25/120（+2 / −1） |
| 11/05 | 46/72 | 45/72（+3 / −4） | 47/72（+2 / −1） |
| 要確認の印なしの誤り | 0 | 0 | 1（ce31c79e#35） |

書き起こしの比べ（1,848 発話）:

| | whisper-1 | gpt-transcribe |
|---|---|---|
| 時間の中央値 | 1.05 秒 | 0.51 秒 |
| 空の出力 | 39 | 99 |
| 費用（同じ数え方。gpt-transcribe の料金は未確認） | 59.5 分・$0.46 | 59.5 分・$0.46 |

`gpt-live-transcribe` はこの受け口で 404（リアルタイム用）。

## Mismatches / Fixes

- 推定器の内容の指紋の試験が、新しい読み込み（`tools.cloud_asr`・`core.cloud_sync`）が推定器の読み込みの木に入ったことで
  失敗した。→ 費用の表を `core/remote_asr.USD_PER_MIN` に、端末トークンの読み込みを `main._cloud_credentials` に移した。
- developers.openai.com はこの環境のネットワークの許可に無く、開けない。→ 提供終了とデータの扱いは検索の要約で書き、
  「公式の本文は未確認」と明記した。

## Test Results

- `ruff check .` 通過、`pytest -m "not slow" -n 4`: 3733 passed, 22 skipped。

## Remaining Gaps

- Google Cloud の登録（オーナー）。
- 公式の本文で、提供終了とデータの扱いを確かめる。
- gpt-transcribe の印なしの誤り（ce31c79e#35）を調べる。料金を確かめる。店舗テストで 1 回 gpt-transcribe で録る。
- 店舗テストで段階 1（テスト中の形）を確かめる（TODO の A・B）。

## Related Commits

- e9c8aa6・b0112d6・c7af66f・この作業ログのコミット。
