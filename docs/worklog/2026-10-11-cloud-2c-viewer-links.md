# Worklog: 店外での閲覧（閲覧リンク, フェーズ 2c）

## Date

2026-10-11

## Scope / Task

ADR-0067 D10 の 2c。クラウドの閲覧リンクの発行・ログイン・お客さんの読み取り、店舗のスタッフ API と画面（QR）、
お客さん向け画面のクラウド用ビルド。

## Changed Files

- `cloud/migrations/0002_viewer_links.sql`（`viewer_links` + `stores.last_seq`）/ `cloud/storage.py`・`cloud/postgres.py`
  （リンクの作成・検索・取り消し、`last_seq`）/ `cloud/views.py`（新規: 店舗ごとの読み取りの写し）/ `cloud/app.py`
  （リンクの発行・取り消し・ログイン、お客さんの読み取り、画面の配信）/ `cloud/tokens.py`（`pkl_`）/ `cloud/__main__.py`
  （`CLOUD_TOKEN_SECRET`・`CLOUD_PUBLIC_URL`）
- `core/cloud_sync.py`（`issue_viewer_link` / `revoke_viewer_link` / `CloudLinkError`）/ `api/server.py`（スタッフ API 2 つ、
  `_qr_image`）/ `api/store_server.py`（`cloud_links`）/ `pyproject.toml`・`requirements-dev.txt`（segno）
- `staff/`（`ViewerLink`・`issueViewerLink`・`revokeViewerLink`・プレイヤー管理の QR カード）→ `api/static/staff/` 作り直し
- `mobile/`（`linkLogin`・読み取りのトークン・`src/cloudLink.ts`・`LinkScreen`・App のクラウドモード・注文の導線を任意に）
  → `api/static/player/` と新しい `cloud/static/player/` を作り直し（`scripts/build_player_web.py` に `--target`）
- テスト: `tests/test_cloud_viewer.py`（新規 20 = メモリ + Postgres）/ `test_cloud_storage.py`（+2×2）/ `test_cloud_app.py`
  （受け口の一覧）/ `test_cloud_sync.py`（スタッフ API +3）/ `test_player_web_build.py`（クラウド用 +2）/ staff・mobile の mock テスト
- 文書: ADR-0067 追記 3 / `docs/usage.md`（閲覧リンクの渡し方）/ `docs/contracts/viewer-api.md` / `docs/cloud-setup.md` /
  CHANGELOG / CLAUDE.md

## Expected Behavior

- スタッフが QR を出す → お客さんが読み取る → 店外から自分のセッション・ハンド・会計だけが見える。
- 取り消し・発行し直しで前のリンクとそのログインは使えない。他人の記録・座っていなかったハンドは 404。

## Implemented Behavior

- 期待どおり。ブラウザ（Playwright, 390×844）で: リンク無し → 案内 / リンクで開く → アドレスバーからリンクが消えて
  セッション一覧 → ハンド一覧 → 会計（注文の導線なし、一部支払いの表示）/ 新しいタブでもログインが続く。
  スタッフ用画面で QR が出ることも確認。

## Test Results

- 全体 2951 passed / 5 skipped / 1 failed（golden `postflop-street-transition` = 手元の環境だけ、CI では通る）。`ruff check .` 通過。staff: typecheck + mock 47 passed / mobile: typecheck + 42 passed。

## Mismatches / Fixes

- 開いたままのページで `#link=` だけ変えると入れなかった（初回の読み込みでしかリンクを見ていなかった）→ `hashchange` でも入る。
- QR が白く出た: segno の `svg_inline` は `xmlns` が無く画像として描けない → `svg_data_uri` を返す（項目名も `qr_image` に）。
- 手元の確認で古いサーバーが同じポートに残っていて古い応答を見た → ポートを変えて確認（手元だけの話）。

## Remaining Gaps

- 2d 公開範囲（`own_and_shown`）: 読み取りの 1 か所（店内とクラウド）で手札を隠す。
- 2e 置く・公開前の確認 10 項目。
- 写しはリクエストのたびに店舗の番号を読む（1 回の問い合わせ）。店舗が大きくなったら、ハンドの書き出しを差分にする。

## Related Commits

- 本 worklog と同じ commit
