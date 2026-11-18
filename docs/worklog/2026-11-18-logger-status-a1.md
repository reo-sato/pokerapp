# Worklog: ADR-0073 A1 — ロガーのロック・状態とお知らせ・iPad のパネル

## Date

2026-11-18

## Scope / Task

オーナー「明日マイクを見ます。進めてください」。ADR-0073 の段階 A1（D2・D3）。

## Changed Files

- `core/logger_status.py`（新規）
  - `LoggerLock`: `StoreWriterLock` と同じ作り。起動のときに 4 回まで取り直す。
  - `LoggerStatus`: 状態のファイルと心拍のスレッド、お知らせ。
  - `read_logger` / `read_notices`: 読む側。ロックは取ってみず、pid と心拍で判断する。
  - 置き場はリポジトリの `logs/`。`POKERAPP_LOGGER_STATE_DIR` で差し替えられる。
- `main.py`
  - `run_cli` を「ロック + 状態」で包み、中身は `_run_cli` にした。
  - 段階を書く: setup → starting（session_id・卓）→ running（マイク・RFID・聞き取り待ちを結び付ける）→
    stopping → stopped。
  - 端末の `●` / `[カード]` / `[聞き取り]` / `[未適用]` / アクションの行を、お知らせにも書く（`_notify`）。
  - `run_gui` もロックと段階だけ。
- `api/server.py`: `GET /api/staff/logger`・`GET /api/staff/logger/notices`（スタッフトークン・読むだけ）。
- `staff/`
  - 型・窓口（HTTP / 偽物）。
  - `src/logger/loggerView.ts`（表示の整形, 試験つき）。
  - `src/screens/LoggerPanel.tsx`。セッション一覧のいちばん上に置いた。
- `api/static/staff/`: ビルドし直した。
- `tests/test_logger_status.py`（新規）
  - ロック。
  - 状態とお知らせ（起動し直しの reset を含む）。
  - 応答なし / 落ちた。
  - API。
  - CLI が書くこと。
  - 2 つ目のロガーは起動しない。
- `tests/conftest.py`: 置き場を試験ごとの一時フォルダに差し替える。
- `docs/contracts/viewer-api.md`・ADR-0073 Follow-up・CHANGELOG

## Test Results

- `pytest -m "not slow" -n 4`: 3757 passed / 遅い組 18 passed。`ruff check .` 通過。
- staff: `tsc --noEmit` 通過、`npm test` 70 passed。`scripts/build_staff_web.py` でビルドし、`tests/test_staff_web*.py` 14 passed。
- 偽のロガーの状態で店舗サーバーの画面を Playwright で開き、パネルの表示を確かめた。ページのエラーは 0。

## Mismatches / Fixes

- 草稿の「`<sid>.notices.jsonl`」は、起動ごとの `logs/logger_notices.jsonl` にした。
  - セッションが決まる前のお知らせも書けるようにするため。
  - 画面は 1 つのファイルを番号で追えばよい。各行に session_id を持つ。
- `acquire_with_retries` の既定の回数が定義の時点で決まり、試験の差し替えが効かなかった → 呼んだ時点で読む。

## Remaining Gaps

- 店舗 PC での確認（RDP で起動したロガーの様子が iPad に出るか）。
- `logger_notices.jsonl` は「ログをまとめる」に入れていない（必要になれば足す）。
- 段階 A2 以降。
