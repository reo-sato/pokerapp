# 2026-10-24 トーナメントの時計を時刻準拠に・18:30 開始のストラクチャー

## Goal

オーナーの依頼: 店のトーナメントのストラクチャー（スクリーンショット 4 枚, レベル 1〜24）の JSON を作る。
ストラクチャーによるブラインドの遷移を**時刻準拠**にする（このトーナメントはレベル 1 が 18:30 開始）。

## Changed files

- `blind_structures_default.json`: 先頭に「18:30 開始トーナメント」（`start_time: "18:30"`, 24 レベル + レベル 6 のあとブレイク 5 分）。
  レベル 1・2 のアンテ「--」は 0。始めの持ち点はスクリーンショットに無いので入れていない。
- `core/blind_structure.py`: `BlindStructure.start_time`・`parse_start_time`・`start_epoch`（今日のその時刻、12 時間より先なら前の日）・
  `TournamentClock.start_at`（レベル 1 を開始時刻に始めた時計を now まで進める）・`status` の `starts_at`（始まる前）。
- `tools/session_setup_ui.py`: 設定の `start_time`（None = ストラクチャーの時刻 / "" = レベルと経過分）。開始時刻の欄・始まる前の表示・
  ストラクチャーの編集の開始時刻の欄。卓の設定 `table` に `start_time` / `start_at`。
- `main.py`: 端末の入力で開始時刻を訊く（`-` = 従来）。`_make_tournament` は `start_at` があれば開始時刻から数え直す。記録の `table` にも残す。
- tests: `tests/test_blind_structure.py::TestStartTime`、`tests/test_session_setup_ui.py::test_tournament_by_start_time` と開始時刻の検証。

## Expected vs implemented

- 期待: 18:30 開始なら 18:50 にレベル 2、20:35〜20:40 ブレイク、20:40 にレベル 7。→ テストで確認。
- 時計の進み方（締め切り方式・次のハンドから反映・`set_level` の記録・replay）は ADR-0071 のまま。開始時刻は起動時の位置を決めるだけ。
- 一時停止・手でのレベル移動は今までどおり使える（使うと時刻からずれる）。

## Remaining gaps

- 店舗 PC に `blind_structures.json`（設定画面で保存したもの）があるとサンプルは読まれない。その場合は設定画面で同じ内容を作るか、
  サンプルの項目を `blind_structures.json` に写す。
- 始めの持ち点は未設定（設定画面の一括の持ち点を使う）。

## Tests

- `pytest -m "not slow" -n auto`: 3496 passed, 22 skipped（手元に無い部品の分）。`ruff check .` 通過。
