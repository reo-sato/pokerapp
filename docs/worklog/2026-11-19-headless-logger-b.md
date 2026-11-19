# Worklog: ADR-0073 B — 画面なしのロガー・終了の制御 / お客さんの履歴に真のアクションだけ

## Date

2026-11-19

## Scope / Task

- オーナー「進めてください」: ADR-0073 の段階 B（D6）。
- 途中のオーナーの依頼: 「今回のテスト中、プレイヤーの履歴 UI には、卓 UI で入力した真のアクションのみを表示する」。

## Changed Files

- `main.py`
  - `--headless --setup <JSON>`。
  - `run_cli(setup=...)`: モードは `headless`。`_headless_session_config` = `build_session_config` で検証し、`log_dir` は
    捨て、前回の設定として保存する。
  - 画面なしは `quit_event` まで待ち、入力も PC のブラウザ（`_open_truth_table`）も使わない。
  - 終了の制御の扱い（`on_stop`）。終了の段階 `transcribing` を状態に書く。制御の読み手は `start_offset=0`。
- `integration/control_consumer.py`: `stop` は聞き取り待ちを通さずに `on_stop` へ渡す。
- `core/control_queue.py`: `stop`。
- `api/server.py`
  - 本文に `session_id` を足した。いま動いているロガー宛ての `stop` は `session_id` が要る（違えば 409 `session_mismatch`）。
  - `show_truth` は `truth_mode` で読む（`"only"`）。
- `core/logger_status.py`: 段階に `transcribing`。
- `core/truth_hand.py`: `truth_view(estimate=)`・`truth_mode`。`api/read_models.py`: `"only"` なら推定を出さない。
- `staff/`: パネルに「終了中（記録は保存済み…）」・起動の入口「画面なし（iPad）」。ビルドし直した。
- `config_default.json`: `show_truth` の説明。
- 試験
  - `tests/test_headless_logger.py`（新規）: 卓の設定で起動 → 制御 → stop で終わる・セッションを閉じる・前回の設定・ロック。
    検証の誤りでは起動しない。端末のロガーへの stop。
  - `tests/test_truth_view_only.py`（新規）。
- 文書: ADR-0073 Follow-up・CHANGELOG

## Test Results

- `pytest -m "not slow" -n 4`: 3791 passed。staff: 71 passed・ビルドの揃い 14 passed。

## 店舗 PC で使うには

- 真のアクションだけ: `C:\PokerHandLogger\venv\Scripts\python.exe C:\PokerHandLogger\tools\set_config.py viewer_api.show_truth only`
  を実行し、店舗サーバーを起動し直す。

## Remaining Gaps

- 段階 C（店舗サーバーが画面なしのロガーを起動・停止・引き継ぐ。iPad の「卓を始める」「終了」）。
- 端末のロガーを iPad から終える（端末の入力を待っているため、いまは未適用で知らせる）。
