# Worklog: 好きな番号の席にプレイヤーを割り当てる

## Date

2026-09-25

## Scope / Task

オーナー「席の番号について、若い順だけでなく、任意の番号の席にプレイヤーをアサインできるようにしてください。」

## Goal

ハンドロガー（`--cli`、GUI も同じ入力）の起動時に、1 番から連番ではなく、実際に座っている席番号（例 2・4・5・8）を
使える。手番・ポジション・RFID の席のリーダーが席番号どおりに対応する。

## Changed Files

- `main.py` — `_parse_seat_list`（人数 1 つ = 1〜N 番 / 2 つ以上 = その席番号。全角・「、」「,」可）、
  `_prompt_session_config` を席番号のリストで回す、ボタン席の入力を「使う席のどれか」に。
- `tests/test_cli_session_layer.py`（`TestArbitrarySeats`）/ `tests/test_poker_engine.py`
  （`test_pokerkit_plays_on_non_contiguous_seats`）。
- docs: `docs/usage.md` / CLAUDE.md（よく使うコマンドの入力説明）/ CHANGELOG / 本 worklog。

## Expected Behavior

- 「席」に `2 5 8` → 席 2・5・8 の名前・スタックを訊き、ボタンは 2/5/8 から選ぶ（空 Enter = 8）。
- 手番とポジションは席番号順に回る（3 人: ボタン 8 → SB 2・BB 5、プリフロップは 8 から、フロップは 2 から）。
- `6` は従来どおり 1〜6 番。

## Implemented Behavior

- 期待どおり。2 つのエンジン（pokerkit / legacy）はもともと席番号の並びで動いていた（`sorted(seats)` と
  席 → 内部番号の対応表）ので、変更は起動時の入力だけ。

## Test Results

- `tests/test_cli_session_layer.py` 36 / `tests/test_poker_engine.py` の追加 1 件 passed。全体 1372 passed、ruff clean。

## Remaining Gaps

- 途中で席が増える・減る（着席・離席）は未対応（エンジンの席はセッションの最初に固定）。今はハンドロガーを起動し直す。
  席番号を言わない運用では、離席した席が手番に残るとアクターがずれるので、必要になったら対応する。

## Related Commits

- （本 worklog と同じコミット）
