# Worklog: 店舗サーバー（画面なし）をサインイン時に自動で動かす

## Date

2026-10-11

## Scope / Task

製品化フェーズ 1（ADR-0063 D8）の 2 つ目「自動起動」。ADR-0065。

## Goal

- 店舗 PC が起動して自動サインインすれば、お客さん・iPad の画面と注文・会計の操作が使える。
- 止まっても自動で戻る。会計の書き手が 2 つになる事故を防ぐ。
- 録音（ハンドロガー）は対象外（対話式・RDP のセッションの中で動かす）。

## Changed Files

- `api/store_server.py` — 新規。`create_store_app`（会計の書き手の API。`--ledger` と共通）/ `server_settings` /
  `keep_running`（卓モニタなどを起動し直す、5 秒から倍々に最大 60 秒）/ `run_store_server`
- `core/store_lock.py` — 新規。`StoreWriterLock`（OS のファイルロック、持ち主を先頭に書く）/ `describe_holder`
- `main.py` — `--serve`、`run_store_server`、`run_ledger_view` がロックを取り `create_store_app` を使う、`--host/--port` の説明
- `config_default.json` — `store_server` の節（9 行の追加だけ）
- `installer/store_server_task.ps1` — 新規。タスク `PokerStoreServer` の登録・起動・確認 / `-Remove`（管理者不要）
- `server_autostart.cmd` / `start_server.cmd` — 新規（ASCII + CRLF）
- `installer/install.ps1` — ショートカット「店舗サーバー 自動起動の登録」、更新の前に止めて後で起動、アンインストールで消す
- `.gitignore` — `store_writer.lock`
- `tests/test_store_server.py` — 新規 16 件。`tests/test_installer.py` — .cmd の一覧に 2 つ追加
- 文書: ADR-0065 / decision-log / CHANGELOG / CLAUDE.md / `docs/usage.md`（「店舗サーバー」の節、iPad の節の準備を整理）/
  `docs/installation.md` / `docs/ui-feature-inventory.md`（C3）

## Expected Behavior

- `python main.py --serve` → `0.0.0.0:8788` で API・`/`・`/staff/`、`8790` で卓モニタ。ログは `logs/store_server.log`。
- 2 つ目の `--serve` や、`--serve` が動いている間の `--ledger` は、持ち主を表示して終わる（終了コード 1）。
- 8788 が使われていれば終了コード 1、ロックは手放す。
- 卓モニタが落ちても店舗サーバーは落ちず、間を空けて起動し直す。

## Implemented Behavior

- 期待どおり（Linux で `main.py --serve --port 18788` を起動: `/api/health`・`/staff/`・`/`・卓モニタ `/`・`/state.json` が 200。
  2 つ目の起動は「会計を書くプロセスが既に動いています: 店舗サーバー（自動起動）（pid …）」で終了コード 1。止めたあとは
  ロックが取れる）。
- SIGTERM で止めたときは uvicorn がシグナルを投げ直してプロセスが終わるため、「止めました」のログは出ない（ロックは OS が外す）。

## Test Results

- `tests/test_store_server.py` 16 passed、`tests/test_installer.py` を含め 71 passed / 5 skipped（pwsh が無いため PowerShell の構文検査は skip）。
- 全体: 2841 passed, 5 skipped, 1 failed（`tests/test_reconstruction.py` の golden `postflop-street-transition` = 変更前の verify-v1 から落ちている推論側の既存の問題, `2026-10-11-staff-web-serving.md` 参照）。`ruff check .` 通過。

## Mismatches / Fixes

- 最初は「PC の起動時に、サインインしていなくても動く」（`-AtStartup` + S4U、登録に管理者）で作った。オーナーから
  「店舗 PC は通電時に自動で立ち上がる」と店舗 PC の手順書（BIOS の通電復帰・自動サインイン・§10 のサインイン時の
  タスク案）を受け、**サインイン時**（`-AtLogOn` + Interactive、管理者不要、`pythonw.exe` で窓なし）に変えた
  （オーナー決定 2026-10-11）。手順書 §10 の古いタスクは店舗 PC に登録されていない（オーナー確認）。
- 最初は卓モニタの起動し直しを 5 秒固定にしていた → ポートが使われていると 5 秒ごとに失敗のログが出るので倍々（最大 60 秒）に。
- `config_default.json` を JSON として書き直すと全体の書式が変わった → 戻して `store_server` の節だけ文字列で差し込んだ。
- ロックファイルをテキストで開いていた → Windows の位置指定があいまいになるのでバイナリに。

## Remaining Gaps

- 店舗 PC（Windows 11）での確認（ADR-0065 の Validation）: 登録 → 再起動 → ログインなしで iPad から `/staff/`、5 分ごとの
  トリガーが無期限に繰り返すこと、プロセスを止めて戻ること、`update.cmd` の前後の停止・起動。
- 起動のたびのバックアップ（30 世代）が、起動と停止を繰り返すと早く押し出される。
- フェーズ 1 の残り: リリース版での更新 / 同期の常駐（店舗サーバーの中で `SyncScheduler` を動かす）。

## Related Commits

- 本 worklog と同じ commit
