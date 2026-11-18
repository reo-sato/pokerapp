# Worklog: sessions.json / players.json を書く前に読み直す（ADR-0073 D5）

## Date

2026-11-18

## Scope / Task

ADR-0073 の監査（Fable）で見つかった、いまある不具合の単独の修正。

## Expected vs Implemented

- 不具合: `SessionRepository._flush` / `PlayerRepository._flush` は、メモリの内容をファイルごと書き換えていた。
  - ロガーは起動時に読んだ内容に、ハンドごとの席の割当を足して書く（読み直さない）。
  - そのため、プレー中に店舗サーバー（iPad）で行った変更が、次のハンドで消えていた。
- 直し:
  - `core/records_lock.py`: 同じフォルダの `records.lock` に OS のロックを掛ける（Windows = `msvcrt.locking`、
    それ以外 = `fcntl.flock`）。
    - セッションとお客さんの記録で 1 つのロック。
    - 同じプロセスの中では入れ子で取れる。
    - 5 秒で取れなければ警告を出してロックなしで進める。
  - 2 つのリポジトリの書き込みの関数（`create_session`・`close_session`・`set_hole_card_visibility`・`assign_seat`・
    `create_player`・`find_or_create`・`rename_player`・`merge_players`・`unmerge`）を `_mutating` で包んだ。
    - 中身は「ファイルのロック → スレッドのロック → 読み直し → 検査 → 変更 → 書く」。
    - 取る順は 1 通りにした（デッドロックしない）。
  - 書き込みの失敗: 次の読み直しでファイルの内容に戻す（`_loaded_stat = None`）。前は失敗を握りつぶして、メモリと
    ファイルがずれた。
  - 同期の merge（`core/sync.merge_snapshot_into`）も同じロックの中で書く。

## Changed Files

- `core/records_lock.py`（新規）・`core/session_repository.py`・`core/player_repository.py`・`core/sync.py`・`.gitignore`
- `tests/test_records_lock.py`（新規）
  - 別のプロセスの変更が残る。
  - 閉じたセッションへの割当は拒む。
  - 書き込みの失敗のあとはファイルの内容から始める。
  - 入れ子と、スレッドの順番。
  - 本物の子のプロセスとの排他。
- `tests/test_estimator.py`（推定器の指紋から外す理由: `core/records_lock.py`）

## Test Results

- `pytest -m "not slow" -n 4`: 3751 passed, 22 skipped。`ruff check .` 通過。

## Remaining Gaps

- 店舗 PC（Windows）での確認。`msvcrt.locking` の動きは `core/store_lock.py` と同じ。
