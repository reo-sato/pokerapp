# Issue 0036: お客さん向けのハンド閲覧が店舗の運用では使えない

## Date

2026-09-25

## Status

Fixed（ADR-0059。店舗 PC での確認は未）

## Severity / Priority

- Severity: High（お客さんが自分のハンドを見る機能が、店舗の構成では**一件も表示できない**）
- Priority: P1（オーナーが次に確認したい機能）

## Area

CLI（`main.py:run_cli`）/ viewer API（`api/server.py`）/ mobile web（`mobile/`）/ 永続化（`core/*_repository.py`, `core/atomic_io.py`）

## Expected Behavior

店舗 PC（ADR-0057）でハンドロガー（`start_logger.cmd` = `--cli`）を動かしている間、お客さんが店の Wi-Fi で
スマホから画面を開き、名前を選ぶと、その日のセッション・ハンド・カードが見える（M1/M2, ADR-0017）。

## Actual Behavior

調査（2026-09-25）で、次の 5 点がそれぞれ単独で表示を塞いでいた。

1. `--cli` は `sessions.json` に席の記録（seat_assignment）を書かない（`IntegrationThread` に `session_repo`
   を渡していない）。viewer の「その人のハンド」は席の記録を起点に探すので、**名前を選んでも何も出ない**。
   起動時に入力した名前は player registry にも入らない（名前の一覧が空）。
2. 画面（`mobile/` の web 版）を配信するものが無い。README は「`dist/` を任意の静的サーバーで配信」。
3. viewer API は `sessions.json` / `players.json` を起動時にしか読まない。hand logger を後から始めると、
   そのセッションは viewer を再起動するまで出ない。
4. staff token 付きでビルドするとハンド訂正が、`player_auth=off` でも PIN / LINE・Google の導線が出る。
5. Windows では別プロセスが開いている間 `os.replace` が `PermissionError` になり、書き込みが失われ得る。

## Reproduction

1. `config.json` の既定（`session_layer.enabled=false`）で `python main.py --cli`、名前を入れて数ハンド。
2. `python main.py --viewer-api` → `GET /api/players` は `[]`（名前が無い）。`sessions.json` も無い。
3. `session_layer.enabled=true` にしても、`--cli` の経路は席を書かない（GUI だけが書く）。

## Root Cause

席の記録（S2.x E1〜E3, ADR-0008）は **GUI の座席設定**を前提に結線され、店舗で採用した `--cli`
（ADR-0057 のランチャ）には結線されていなかった。viewer API と mobile は「hand logger と同じプロセスか、
API の後に hand logger が書かない」前提（起動時に一度読む・別の静的サーバーで配る）のままだった。

## Fix

ADR-0059:

- `--cli`: `session_layer.enabled=true` なら session を作り、起動時の名前を player に結び付けて席を記録。
  `seat <席> <名前>` / `seat <席> -` で次のハンドから席替え。終了で session を閉じる。
- viewer API: `/` で画面を配信（同じ origin）、`/api/` の要求ごとに変わっていれば読み直す、
  `--host` / `--port`。ランチャ `start_viewer.cmd` + ショートカット。
- 画面のビルドをリポジトリに含める（`scripts/build_player_web.py` → `api/static/player/`。API = `/`、
  PIN / LINE・Google なし、staff token なし）。ビルドし直し忘れは CI で落とす。
- Windows の共有違反を短く再試行。`tools/set_config.py` と BOM 付き config の読み込み。

## Regression Test

- `tests/test_cli_session_layer.py`（名前 → 席の記録 / 席替えは次のハンドから / 同じ名前は同じ人 /
  終了で session を閉じる / 無効時は従来どおり / 共有違反の再試行）
- `tests/test_viewer_api_store.py`（API の起動後に書かれたセッション・ハンドが出る / 画面の配信 /
  no-cache / `/api/` の 404 は JSON / host・port）
- `tests/test_player_web_build.py`（ビルドの完全性・設定・staff token なし・ソースとの一致）
- `tests/test_set_config.py` / `tests/test_installer.py::TestLaunchers`

## Related

- ADR-0059 / ADR-0057（Stage 2）/ ADR-0017（M1/M2）/ ADR-0008（E1〜E3）/ ISSUE-0019（name-pick）
- `docs/worklog/2026-09-25-store-player-viewer.md`
