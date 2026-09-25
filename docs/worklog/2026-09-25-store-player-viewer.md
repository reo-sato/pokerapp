# Worklog: 店舗でお客さんが自分のハンドを見る（ADR-0059 / ISSUE-0036）

## Date

2026-09-25

## Scope / Task

オーナーの依頼「ハンドログをお客さんが見る UI のチェックに移りたい」。既存のお客さん向け画面（`mobile/` の
web 版 = M2 + viewer API = M1）を、店舗の構成（ADR-0057 の店舗 PC + `--cli`）で実際に開ける状態にする。

## Goal

店舗 PC でハンドロガーを動かしながら、お客さんが店の Wi-Fi で `http://<PC の IP>:8788/` を開き、名前を選ぶと
その日のセッション・ハンド・カードが見える。店舗 PC の追加作業はコマンドのコピペだけ（Node 不要）。

オーナーの決定（AskUserQuestion）:

- 公開範囲: 「3 を前提に、後々セッションごとに公開範囲を決められるように」= 当面は全員分のホールカード。
- 席の入力: 「ハンドロガーで入力（推奨）」。

## Changed Files

- `main.py` — `--cli` の session layer（`_open_session_layer` / `_close_session_layer`）、`seat` コマンド
  （`_parse_seat_command`）、起動時の名前入力に `named`、`--host` / `--port`、`--ledger` の組み込み API にも画面。
- `integration/engine.py` — `rename_seat`（ハンド途中は次のハンド開始まで保留）。
- `core/game_state.py` / `core/poker_engine.py` — `set_player_name`。
- `core/player_repository.py` — `find_or_create` / `reload_if_changed` / `_parse`。
- `core/session_repository.py` — `reload_if_changed`（player も連鎖）/ `_parse`。
- `core/atomic_io.py` — `file_stat`、`os.replace` と読み込みの再試行（Windows の共有違反）。
- `core/config.py` — BOM 付き UTF-8 を読む。
- `api/server.py` — `PLAYER_WEB_DIR`、`_PlayerWebFiles`（入口 no-cache）、読み直しの middleware、
  `create_app(player_web_dir=)`、`run_server(host, port)`。
- `api/static/player/` — お客さん向け画面のビルド（新規, コミット）。
- `scripts/build_player_web.py` — ビルド + `--check`（新規）。
- `mobile/App.tsx` / `mobile/src/screens/PlayerSelectScreen.tsx` — staff token が無ければ訂正、
  `EXPO_PUBLIC_PLAYER_AUTH=off` なら PIN / サインアップを出さない。
- `mobile/app.json`（name / `web.lang=ja` / `themeColor`）/ `mobile/public/index.html`（暗い背景・日本語の noscript）。
- `start_viewer.cmd` / `installer/install.ps1`（ショートカット 5 つ目）/ `tools/set_config.py`（新規）/ `.gitattributes`。
- tests: `tests/test_cli_session_layer.py` / `tests/test_viewer_api_store.py` / `tests/test_player_web_build.py` /
  `tests/test_set_config.py`（新規）/ `tests/test_installer.py`。
- docs: ADR-0059（新規）/ ISSUE-0036（新規）/ ADR-0057 追記 / `docs/contracts/viewer-api.md` / `docs/usage.md` /
  `docs/installation.md` / `mobile/README.md` / CLAUDE.md / CHANGELOG / decision-log / 本 worklog。

## Expected Behavior

- `session_layer.enabled=true` の `--cli` は起動時の名前を player に結び付け、ハンドごとに席を記録する。
  名前を入れない席は結び付けない。`seat` で次のハンドから席替え。終了で session を閉じる。
- viewer API は `/` で画面、`/api/` で API。hand logger が後から書いたものも再起動なしで見える。
- 画面にログイン・サインアップ・訂正の導線が出ない。店舗 PC に Node は要らない。
- 既定（`session_layer.enabled=false`）の `--cli` は従来どおり。

## Implemented Behavior

調査で塞いでいた 5 点（ISSUE-0036）をすべて解消した。ヘッドレス Chromium（390×844, `ja-JP`）の通し:

1. 作業コピー（scratch）に `session_layer.enabled=true` の config を置き、`--cli` を入力列で駆動
   （3 席: 太郎 / 花子 / 空 Enter、2 ハンド。1 ハンド目の後に `seat 3 次郎`）。
   → hand 1 は席 1・2 だけ記録、`席3 を 次郎 にしました（次のハンドから）`、hand 2 は 3 席とも記録。
2. `python main.py --viewer-api --host 127.0.0.1 --port 8799` → `/` は 200・`cache-control: no-cache`、
   `/api/players` は 3 名、未知の `/api/nope` は JSON 404。
3. 名前 → セッション（`2026-09-25_054942 ・ blinds 50/100 ・ 2 hands`）→ ハンド一覧（`Hand #1 -300` /
   `Hand #2 +350`）→ ハンドの詳細（ストリートごとのアクション・ポット・収支・勝者）。PIN / サインアップ / 訂正の
   導線なし、コンソールエラー 0。
4. viewer を起動したまま 2 回目の `--cli` を実行 → 新しいセッションが再起動なしで出る、名前は重複しない。

## Mismatches

- 最初の実装では `q` の後もセッションが「進行中」のまま残った → 終了で close するよう追加（テスト追加）。
- お客さん画面の目視で気づいた細部（今回は変えていない。オーナーの確認後に判断）:
  - プリフロップの見出しの「ポット 0」（ブラインドを含まない。共有リプレイ `shared/hand_replay` の `potStart` の仕様）。
  - 日時が ISO のまま（`2026-09-25T05:49:42.842`）、セッション名が起動時刻（`2026-09-25_054942`）。
  - 「← player 選択」の表記、「会計を見る」リンク（会計を使わない店では空）。
  - 名前の一覧はこれまで入力した全員（絞り込み無し）。

## Fixes

- CLI の session layer / `seat` / 終了時 close、viewer の画面配信・読み直し・no-cache・`--host`/`--port`、
  ビルドの同梱と drift 検知、Windows の再試行、`tools/set_config.py`、BOM 許容。

## Test Results

- `tests/test_cli_session_layer.py` 19 / `tests/test_viewer_api_store.py` 8 / `tests/test_player_web_build.py` 8 /
  `tests/test_set_config.py` 10 / `tests/test_installer.py`（pwsh の構文解析 + `-DryRun` 込み）all passed。
- 全体: `pytest tests/ --ignore=tests/test_vision.py` 1297 passed、`ruff check .` clean。
- `mobile/`: `npx tsc --noEmit` exit 0。

## Remaining Gaps

- 店舗 PC（Windows）での確認: `set_config` → 更新 → `start_logger` で名前入力 → `start_viewer` → お客さんのスマホ。
- future: セッションごとの公開範囲（ADR-0059 D7）/ 名前一覧の絞り込み / 上の表示の細部。
- `sessions.json` の書き手が 2 つ（`--cli` + `--ledger` の staff API）になる運用では更新を失う窓が残る。

## Related Commits

- （本 worklog と同じコミット）
