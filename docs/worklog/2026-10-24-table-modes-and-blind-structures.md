# 2026-10-24 卓のモード・BB アンテの額・ストラクチャーによるブラインドの自動上昇・起動時の設定画面（ADR-0071 第 1 段）

## 目的

推定器 v2 の事前 P(H) の議論で、オーナーが「リングゲームとトーナメントのモードを実装して区分けする。同時に ante の有無と、
事前に登録したトーナメントストラクチャーに沿って時間でブラインドを自動変更する機能を作る」と決めた
（`docs/estimator-v2/README.md` §3.6 決定 5）。計画の段階で「cli に全部手打ちするのめんどくさいから、モード設定などを起動時に
入力する UI も欲しい」→ PC のブラウザの設定画面・前回の値を覚える、に決まった。

オーナーの決定（10/24）: アンテは BB アンテで額は BB と別（全員アンテなし・リングゲームはアンテなし）/ 時計の操作は第 1 段は
CLI と PC の画面、iPad は第 2 段 / レベルの時間が来たら次のハンドから上げる / 起動時の設定はブラウザの画面・前回の値を覚える。

## 変えたファイル

- 新規 `core/blind_structure.py`: `BlindLevel` / `BlindStructure` / `parse_structure`（検証）/ `BlindStructureStore`
  （`blind_structures.json`、無ければサンプル `blind_structures_default.json`）/ `TournamentClock`（締め切り方式・一時停止・
  複数レベルをまたぐ・最後で止まる・ブレイク中は前のレベルのブラインド・`status` は絶対時刻）。5 月の未マージのブランチ
  `claude/add-tournament-timer-n5OGx` のモデルを移した（レイトレジストレーション・アドオン・GUI の表示は移していない）。
- 新規 `blind_structures_default.json`（標準 20 分・ターボ 10 分の 13 レベル + ブレイク 2）。`.gitignore` に
  `blind_structures.json`、`installer/install.ps1` の保持に追加（`tests/test_installer.py`）。
- `core/poker_engine.py`: `set_bb_ante(True | 額 | False)`、`_raw_antes(stacks)` で BB の人の持ち点が足りなければアンテを減らす
  （ブラインドが先 = TDA）、`bb_ante` はそのハンドで払った額。`core/game_state.py` の型。
- `integration/engine.py`: `table_mode` / `tournament` / `on_clock_change`、周期の `_check_tournament_clock`、`_emit_level`
  （`set_level` を記録して反映）、`_handle_set_level`、`_handle_clock`（status / pause / resume / next / prev / set）、
  `ante <額>`、`table_info()`（卓モニタ）、記録の `blinds.level`・`table_mode`。
- `core/hand_log.py`: `HandSummary.table_mode`（None なら出さない）。`core/table_state.py`: `TableState.game`。
- `output/event_recorder.py`: `record_setup`。`output/json_writer.py`: `set_table`。`integration/replay.py`: `load_events` は
  `session_setup` を飛ばす・`load_session_setup`・`replay_events(table_mode=)`。
- `integration/world_replay.py` / `integration/estimator.py` / `core/truth_hand.py`: 記録の `bb_ante` の額をそのまま渡す。
- 新規 `tools/session_setup_ui.py`: 設定画面（`build_session_config` の検証・前回の設定・時計の保存と「前回の時計から続ける」・
  ストラクチャーの編集・時計のタブ・`wait_for_setup` は端末の `t` で端末の入力へ）。
- `main.py`: `_session_config`（CLI と GUI）・`_prompt_table_mode`（端末の入力の先頭にモード）・`_make_tournament`・
  `_record_table`・`_table_kwargs`・`clock` のコマンド・`ante <額>`。端末が対話でない起動は端末の入力のまま。
- `tools/table_monitor.py`: レベル・ブラインド・残り時間（絶対時刻から PC の時計で）・次のレベル・ブレイクまで。
- schema: `hand` 1.8（`table_mode`・`blinds.level`）、`reconstruction_event` 0.21（`session_setup`・`set_level`・`clock`）。
- `config_default.json`: `session_setup`（enabled / port / open_browser）。
- テスト: `tests/test_blind_structure.py`（25）・`tests/test_tournament_clock.py`（9: 次のハンドから・ブレイク・時計の操作・
  replay で同じ記録・schema・端末の入力でトーナメント）・`tests/test_session_setup_ui.py`（18）・`tests/test_table_state.py`
  `TestGameInfo`（3）。既存の CLI のテストの入力の先頭にモードの答え（空 = リングゲーム）を足した。推定器の指紋のテストに
  `core/blind_structure.py`（ライブの時計と表示の文言, 再生は記録の `set_level` で同じ）を理由つきで足した。
- 文書: ADR-0071、`docs/usage.md`（起動時の卓の設定・`ante` / `clock`）、CLAUDE.md、CHANGELOG、decision-log、台帳、
  `docs/estimator-v2/README.md` §3.7。

## 期待と実装

| 期待 | 実装 |
|---|---|
| 起動時の設定を手打ちしない | ブラウザの画面。前回の値で開くので変えない日は「始める」だけ |
| トーナメントはレベルの時間で上げる・次のハンドから | ロガーの時計が `set_level` を記録して反映（ハンド中は保留） |
| アンテは BB アンテ・額は BB と別 | `set_bb_ante(額)`、レベルごとの `ante`、`ante <額>` |
| 再生で同じ記録 | replay は時計を持たず記録の `set_level` で同じ（テストで一致） |
| 記録でリングゲーム / トーナメントを分ける | `table_mode`・`blinds.level`・`session_setup` |

## 試したこと

- ブラウザ（コンテナの Chromium + Playwright）で通し: 前回の値で開く → トーナメント・レベル 3・経過 5 分 → ストラクチャーの
  複製・ブレイクとレベルを足して保存 → 席を 1 つにして「始める」= 席のエラー → 2 席で始める → 時計のタブ（残り 15:00 から
  数える）→ 一時停止。最初の通しで「保存で名前を変えると設定のストラクチャーが消えて『選んでください』になる」不具合が
  見つかり、選び直し・名前の追従・「複製して新しく作る」を足した。
- 卓モニタを画面で確認（レベル 3 300/600 アンテ 600・残り・次 400/800・ブレイクまで・このハンドのブラインド）。
- BB の人の持ち点が 350・150（BB 200・アンテ 300）: ブラインドが先に入り、アンテは残り（150 / 0）。heads-up も BB に付く。

## 試験

- `pytest -m "not slow" -n auto`: 3459 passed・22 skipped。`ruff check .`: 問題なし。
- `pytest -m slow`: 13 passed・5 skipped。E の聞き取りの直しを取り込んだあとの速い組: 3463 passed・22 skipped。
- 推定器の物差し（`TZ=Asia/Tokyo python tools/bench_hands.py`、取り込み後）: 内容の指紋 `dc64e7513e33`（動いた）・値の指紋
  `ace16aaa3de7`（同じ）。店舗 **140/174**・良くなった 28・悪くなった 0・要確認なしの誤り 2 → 2（新しく 0）、台本 **29/30**、
  真のアクションは前の記録と同じ = **数字は変わらない**（モードの既定はリングゲーム・アンテなしで記録を変えない）。

## 残り（planned）

- 第 2 段: iPad / スタッフ API（ストラクチャーの編集 `GET/PUT /api/staff/blind-structures`・時計の操作を control queue で・
  セッション作成のモード・スタッフの `set_blinds` / アンテ・sessions.json の `game_type`）。
- 第 3 段: 推定器 v2 の局面の特徴に卓の種類、真のアクション入力の画面にレベル・アンテ。
- 店舗 PC で確かめる: RDP の中でブラウザが開くか（`webbrowser.open`）、端末の `t`（Windows の `msvcrt`）。
