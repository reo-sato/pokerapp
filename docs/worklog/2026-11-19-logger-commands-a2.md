# Worklog: ADR-0073 A2 — 端末の操作を iPad の制御と共通に

## Date

2026-11-19

## Scope / Task

オーナー「進めてください」（段階 0 の結果のあと）。ADR-0073 の段階 A2（D4）。

## Changed Files

- `integration/logger_commands.py`（新規）
  - `LoggerCommands.run_line` / `run_text`。端末の入力のループにあった処理（副作用 + AudioEvent の列）を移した。
  - `CommandResult`（events / messages / ok / stop）。
  - `control_line`: 制御 → 同じ意味の端末の 1 行（検査つき）。
  - `normalize_command` / `parse_name_command` も `main.py` から移した。`main` は前の名前で読み込む。
- `main.py`
  - 入力のループは `commands.run_line` を呼ぶだけにした。
  - 制御の読み手に `commands` と結果の知らせ（`[iPad] …` と `_notify("control" | "unapplied")`）を渡した。
- `integration/control_consumer.py`: `commands` があれば、行の形がある制御と `text` を共通の処理で扱う。休み / 参加・台本・
  持ち点は今までの翻訳のまま。
- `core/control_queue.py`: `VALID_CONTROL_TYPES` に seat_player / reduce_stack / set_ante / clock / reader_restart / text。
- `api/server.py`
  - 制御の本文に seats / name / value / op / text を足した。
  - 引数の検査を `_control_args`（`control_line` と同じ検査）にまとめた。
  - `POST /api/staff/logger/control` を足した。
- 試験
  - `tests/test_logger_commands.py`（新規）: 14 の制御が端末と同じ AudioEvent になる・使えない引数・副作用・打ち込み・
    制御の読み手。
  - `tests/test_logger_status.py`: いま動いているロガー宛ての受け口。
  - `tests/test_cli_session_layer.py`: 関数の移し先から読む。
- 文書: `docs/contracts/viewer-api.md`（制御の type の表）・ADR-0073 Follow-up・CHANGELOG

## Expected vs Implemented

- 端末の動き（表示の文・積む AudioEvent）は変えない → 既存の CLI の試験はそのまま通った。
- 制御の勝者は、前は席の項目つきの AudioEvent だった。いまは端末と同じ（席の項目なし、raw_text から席を読む）。
  - engine はどちらも同じに扱う。
  - 前の記録の再生は、記録の行のとおりで変わらない。

## Test Results

- `pytest -m "not slow" -n 4`: 3784 passed / 遅い組 18 passed。`ruff check .` 通過。

## Remaining Gaps

- iPad の画面のボタン（段階 A3）。
- `stop`（終了）と段階のある終了・制御のファイルを最初から読む（段階 B）。
- 真のアクション入力（8791）の制御の書き方を 1 つにする（段階 C）。
