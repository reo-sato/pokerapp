# Worklog: 店舗 PC → クラウドの送り手（フェーズ 2b）

## Date

2026-10-11

## Scope / Task

ADR-0067 D10 の 2b。送り手（変わったレコードだけ・状態・常駐）、つなぐ道具、`/api/health` の `cloud`。ADR-0063 D8 の
フェーズ 1 の残り「同期の常駐」もこれで済む（相手がクラウド）。

## Changed Files

- `core/cloud_sync.py` — 新規（`collect` / `CloudSyncer` / `SyncState` / `HttpTransport` / `syncer_from_config`）
- `tools/cloud_link.py` — 新規（link / reissue / status / push / unlink）
- `api/store_server.py` — `start_cloud_sync`（`keep_running` で常駐）、`create_store_app(cloud_status=…)`
- `api/server.py` — `create_app(cloud_status=…)` → `/api/health` の `cloud`
- `config_default.json`（`cloud` の節）/ `.gitignore` / `core/backup.py`（`cloud_credentials.json`）/
  `installer/install.ps1`（`$PreservedFiles` に 2 つ）
- `tests/test_cloud_sync.py` — 新規 19 件
- 文書: ADR-0067 追記 2 / `docs/cloud-setup.md` §3 / `docs/contracts/viewer-api.md`（health）/ CHANGELOG / CLAUDE.md

## Expected Behavior

- つないだ店舗 PC の店舗サーバーが、60 秒ごとに変わったレコードだけを送る。2 回目以降は何も送らない。
- 失敗しても店内は止まらず、間を空けて試す。トークンはどこにも表示しない。

## Implemented Behavior

- 期待どおり。手元で `python -m cloud`（Postgres 16）に `tools/cloud_link.py link` → 送信 14 件 → 2 回目 0 件 →
  `status` でクラウドの件数まで確認（HTTP 越し）。

## Test Results

- `tests/test_cloud_sync.py` 19 passed。関連（cloud・store_server・installer）121 passed / 5 skipped。全体 2922 passed / 5 skipped / 1 failed（golden `postflop-street-transition` = 手元の環境だけで落ちる既存のもの、CI では通る）。`ruff check .` 通過。

## Mismatches / Fixes

- `cloud_sync_state.json` が消えて時計が戻ると、ハンドの変更が「古い番号」として捨てられた（テストで発見）→ 起動して最初に
  送る前にクラウドの最大の番号に合わせる。クラウドが「古い」と答えた種類は受け取り済みにせず送り直す。
- 番号の確認に失敗したとき「送れていない件数」が 0 と出た → 先に送るものを数えてから確認する順に。
- 手元の試験用 Postgres が会話の切れ目で止まっていた → 起動し直した（手元だけの話）。

## Remaining Gaps

- 店舗 PC（Windows）での確認（2e でクラウドを置いてから）。
- スタッフ画面にクラウドの状態を出す（いまは `/api/health` と `cloud_link.py status`）。
- 2c 閲覧リンク / 2d 公開範囲 / 2e 置く。

## Related Commits

- 本 worklog と同じ commit
