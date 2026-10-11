# Worklog: クラウドの骨組み（フェーズ 2a）

## Date

2026-10-11

## Scope / Task

ADR-0067 D10 の 2a。クラウドのアプリ・保存（メモリ / Postgres）・migrations・端末トークン・管理の受け口・同期の受け口。

## Goal

- 店舗 PC からの push を、契約の形を確かめて店舗ごとに保存できる。店舗をまたいで混ざらない。
- クラウドにはスタッフ・会計を書く受け口が無い。トークンはハッシュだけ保存し、ログに出さない。
- メモリ版と Postgres 版が同じテストを通る。

## Changed Files

- `core/cloud_payload.py` — 新規（種類・キー・送る前に落とす項目・指紋。送り手 2b と共通）
- `cloud/__init__.py` / `__main__.py`（`python -m cloud`）/ `app.py` / `ingest.py` / `records.py` / `storage.py` /
  `postgres.py` / `tokens.py` / `migrations/0001_init.sql` — 新規
- `tests/cloud_helpers.py` / `tests/test_cloud_storage.py`（19, メモリ + Postgres）/ `tests/test_cloud_ingest.py`（14）/
  `tests/test_cloud_app.py`（18）— 新規
- `.github/workflows/ci.yml`（サービスの Postgres 16 + `POKERAPP_TEST_DATABASE_URL`）/ `requirements-dev.txt`（psycopg）/
  `pyproject.toml`（extra `cloud`、packages に `cloud`）
- 文書: ADR-0067 追記 1 / CHANGELOG / CLAUDE.md（ディレクトリ構成・残作業 #10・コマンド）

## Expected Behavior

- `POST /api/cloud/v1/sync/push`（端末トークン）: 形の合うレコードを保存、合わないものは理由を返す。同じものの再送は何も変えない。
- ハンド・店舗の設定は送り手の番号が新しい方、会計の明細は append-only、session などは `core/sync.py` の解決関数（店舗 PC の値を優先）。
- 管理の受け口は管理トークン（32 文字以上）が無ければ 404。

## Implemented Behavior

- 期待どおり。手元で `python -m cloud`（Postgres 16）を起動し、health → 店舗の作成 → push → status を curl で確認。
  migration は起動時に当たり、2 回目は何もしない。トークンはログに出ていない。

## Test Results

- 新しい試験 51 件（Postgres 版を含む）通過。全体: 2859 passed / 5 skipped（`test_reconstruction.py` を除く）+
  `test_reconstruction.py` 45 passed / 1 failed（golden `postflop-street-transition` = 変更前から落ちている推論側の既存の問題）。
  `ruff check` 通過。

## Mismatches / Fixes

- ADR の本文の形（hands / corrections を別の項目）→ `records` の種類にそろえた（追記 1）。
- 解決関数の引数の順番: cloud にあるものを先にすると店舗 PC の label の変更が写しに入らない → 届いた方を先に。
- 接続できないとき pool が待ち続けた → `timeout=15`。
- Postgres の並び順が Python と違う可能性 → `order by key collate "C"`。
- 手元の試験用 Postgres を scratchpad に置いたら権限が戻されて止まった → postgres ユーザーのホームに置いた（手元だけの話）。

## Remaining Gaps

- 2b 店舗 PC の送り手（`load_record` の結果 = 店舗 PC が見せるものを送る、`cloud_sync_state.json`、常駐、`tools/cloud_link.py`）。
- 2c お客さんの読み取り（店内の `api/read_models.py` と同じ結果になることを試験で確かめる）と閲覧リンク。
- 2d 公開範囲: `core/sync.py` の `_resolve_session` は知らない項目を落とすので、session に `hole_card_visibility` を足すときに直す。
- 2e Dockerfile・Cloud Build・Secret Manager・D9 の確認。

## Related Commits

- 本 worklog と同じ commit
