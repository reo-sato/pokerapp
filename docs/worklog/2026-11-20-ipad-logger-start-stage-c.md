# 2026-11-20 iPad からハンドロガーを起動・終了する（ADR-0073 段階 C）

## goal

オーナー「段階 C を始める」。iPad（スタッフ用画面）の操作だけでロガーを起動・終了する。

## changed files

- 新規 `core/logger_supervisor.py`（`LoggerSupervisor.start` / `watch_once` / `run_forever` / `crash`）。
- `core/logger_status.append_notice`（見張り役がお知らせを足す）。
- `api/store_server.py`（`start_logger_supervisor`・`create_store_app(logger_supervisor=)`）。
- `api/server.py`（`GET /api/staff/logger/setup`・`POST /api/staff/logger/start`・`/api/staff/logger` に `can_start`・`crash`）。
- `main.py`（`run_cli` が終了コードを返す・`--headless` の失敗は 1）。
- `installer/install.ps1`（`Test-LoggerRunning`・`-Force`・`POKERAPP_FORCE_UPDATE=1`）。
- `config_default.json`（`store_server.logger_start`）。
- staff:
  - `LoggerStartScreen`（新規）、`logger/setupForm.ts`（新規 + 試験）、`LoggerPanel`（卓を始める・終了）。
  - リポジトリ（`getLoggerSetup` / `startLogger` / `stopLogger`、`StaffApiError.errors`）。
  - 偽物版（ロガーは止まった状態から）、e2e 1 本、`api/static/staff` を作り直し。
- 文書: ADR-0073（Follow-up）・`docs/contracts/viewer-api.md`・`docs/usage.md`・CHANGELOG。

## expected vs implemented

- 起動: 検証（`build_session_config`、結果は捨てる）→ `logs/logger_setup.json` → `main.py --headless --setup`。
  - 子の扱い: `CREATE_NO_WINDOW`、`PYTHONUTF8=1`、出力は `logs/logger_child.log`。
  - 断るとき: 動いていれば 409、10 秒以内は 429。
- 見張り（3 秒ごと）:
  - crashed の pid ごとに 1 回、お知らせを足して live_hand を消す。記録は `logs/logger_crash.json`。
  - 引き取りは状態のファイルで見えていれば何もしない（ログに 1 行）。
- 終了: 既存の制御 `stop`（session_id 必須）。端末で起動したロガーには「終了」を出さない（端末の q）。
- 計画との差: 本物の子のプロセスを起動する遅い組の試験は作らなかった。
  - 子は repo の `config.json` と `logs/` を使うため。
  - 画面なしのロガーの通しは `tests/test_headless_logger.py`（同じプロセス）で確かめている。
  - 店舗 PC で確かめる。

## tests

- `tests/test_logger_supervisor.py`（6）・`tests/test_installer.py`（更新のガード）。
- staff: `npm test` 76・`tsc`・Playwright e2e 9（新しい 1 本を含む。この環境のブラウザの版の違いは、一時の設定で
  `/opt/pw-browsers/chromium` を指して回した）。
- `ruff`・`pytest -m "not slow" -n 4`・`pytest -m slow -n 4`。

## remaining

- 店舗 PC:
  - iPad で始める → 1 ハンド → 終了。
  - 店舗サーバーのタスクを止めてもロガーが残るか。
  - 動いている間の update.cmd が断られるか。
- あとに回したもの（ADR-0073 Follow-up）: D10 の印・ストラクチャーの編集・player_id・強制終了・8791 の認可・A3。
