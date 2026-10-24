# ADR-0071: 卓のモード（リングゲーム / トーナメント）・BB アンテの額・ストラクチャーによるブラインドの自動上昇・起動時の設定画面

## Status

Accepted（2026-10-24。第 1 段 = ロガー・PC の設定画面・卓モニタは実装済。第 2 段 = iPad / スタッフ API、第 3 段 = 推定器・
真のアクションの入力の残りは planned）

## Date

2026-10-24

## Context

- 推定器 v2 の事前 P(H) の議論（`docs/estimator-v2/README.md` §3.6）で、オーナーが決めた: 店の卓はリングゲームも多く、
  プレイヤーの行動は卓の種類で違う → **リングゲームとトーナメントのモードを実装して記録を分ける**。あわせて**アンテの有無**と、
  **事前に登録したトーナメントのストラクチャーに沿って時間でブラインドを自動で上げる**機能を作る（決定 5）。
- いま: ブラインドは起動時の端末の質問で一度決めるだけ。途中の変更は `blinds <SB> <BB>`（次のハンドから）。アンテは
  `ante on|off` の BB アンテで**額は BB と同じ**（2026-10-19）。卓の種類・レベルの概念は無く、起動時の設定は `events.jsonl` に
  載らない。卓モニタにブラインドの表示は無い。
- オーナー（10/24）:「cli に全部手打ちするのめんどくさいから、モード設定などを起動時に入力する UI も欲しい」→ PC のブラウザの
  設定画面・前回の値を覚える。アンテは BB アンテで額は BB と別、リングゲームはアンテ無し。時計の操作は第 1 段は CLI と PC の画面、
  iPad は第 2 段。レベルの時間が来たら次のハンドから上げる。
- 5 月の未マージのブランチ `claude/add-tournament-timer-n5OGx` にストラクチャーと時計のモデルがあった（GUI スレッドから直接
  ブラインドを変える古いつなぎ方）。

## Decision

### D1. ストラクチャー（`core/blind_structure.py`）

- `BlindLevel`（SB / BB / アンテ / 分 / ブレイク）・`BlindStructure`（名前つき・始めの持ち点は任意）。レベルの番号はブレイクを
  除いて 1 から。アンテは **BB アンテの額**（0 = 無し）。全員アンテは持たない。
- 検証: 名前 1〜60 文字・1〜100 行・SB ≥ 1・SB ≤ BB・アンテ ≥ 0・1〜600 分・通常のレベルが 1 つ以上・BB は前のレベル以上。
- 置き場: `blind_structures.json`（店が編集。`.gitignore`・更新で保持 = `installer/install.ps1` の `$PreservedFiles`）。無ければ
  コミット済みのサンプル `blind_structures_default.json`（標準 20 分・ターボ 10 分）を読む（`config_default.json` と同じ形）。
  `BlindStructureStore` は `core/menu.py` と同じ形（ロック・変われば読み直す・atomic な書き込み）。

### D2. 時計はロガー（IntegrationThread）が持ち、変化は入力として記録する

- `TournamentClock`: 締め切り方式（レベルを始めた時刻 + 長さ）。一時停止は経過を持つ。`tick(now)` は何度呼んでもよく、
  複数のレベルをまたげる。最後のレベルで止まる。ブレイク中のブラインドはその前の通常のレベル。
- `IntegrationThread(table_mode=, tournament=, on_clock_change=)`: 周期の処理で時計を見て、ブラインドが変わるレベルに入ったら
  **`set_level` の入力**（`amount` = レベルの番号、`raw_text` = `"SB/BB ante=額"`）を作り、記録（`events.jsonl`）してから
  反映する（`set_blinds` / `set_bb_ante` と同じ = **ハンドの途中なら次のハンドから**）。始めのレベルも起動時に同じ入力で出す。
- **replay は時計を持たない**: 記録した `set_level` で同じブラインド・アンテになる（決定性 = `tests/test_tournament_clock.py`）。
- 時計の操作は `clock` の入力（`status` / `pause` / `resume` / `next` / `prev` / `set <レベル> [経過分]`）。CLI の `clock …` と
  設定画面の「時計」がロガーの queue に積む（GameStateManager はロックを持たないので状態の変更はロガーのスレッドで, ISSUE-0012）。
- 時計の状態は変わるたびに `logs/<sid>.tournament.json` と `logs/tournament_clock.json`（最後の時計）に書く。ロガーを起動
  し直したときは設定画面の「前回の時計から続ける」でレベルと経過分を埋める（16 時間まで）。

### D3. BB アンテの額を BB から切り離す（`core/poker_engine.py`）

- `set_bb_ante(True)` = BB と同額（従来どおり、BB を変えればアンテも変わる）/ 整数 = その額 / `False`・0 = 無し。
  CLI は `ante on|off|<額>`。
- **BB の人の持ち点がブラインドとアンテの両方に足りなければブラインドが先**（TDA の規則。pokerkit はアンテを先に取るので、
  アンテを「持ち点 − BB」までに減らして渡す）。記録の `blinds.bb_ante` は払った額。
- 推定器・世界の再生（`replay_world(bb_ante=)`）・真のアクションの再生（`core/truth_hand.py`）は記録の額をそのまま渡す
  （前の記録は BB と同額なので結果は同じ）。

### D4. 記録（schema は additive）

- `events.jsonl` の先頭に**卓の設定** `{"type": "session_setup", table_mode, sb, bb, ante, structure, level, elapsed_min, seats,
  first_button}`（`reconstruction_event` **0.21**）。再生の入力ではない（`load_events` は飛ばし、`load_session_setup` が読む）。
  audio の action に `set_level` / `clock`。
- `logs/<sid>.json` に `table`（同じ中身）。ハンドに `table_mode`、`blinds.level`（トーナメント）（`hand` **1.8**）。
  モードを決める前の記録・台本のハンド（`--script`）・replay の既定は従来どおり（出力に含めない）。
- sessions.json の session の `game_type`（会員の画面に出す）は第 2 段（スタッフのセッション作成と一緒に）。

### D5. 起動時の設定画面（`tools/session_setup_ui.py`）

- `main.py --cli`（`start_logger.cmd`）と GUI は起動すると PC のブラウザで設定画面を開き、「始める」まで待つ。
  標準ライブラリの HTTP サーバーで **`127.0.0.1:8794` だけ**（RDP の中のブラウザ。使われていれば次の番号）。
- 項目: モード・リングゲームの SB / BB・トーナメントのストラクチャー / 始めるレベル / 経過分・使う席（押して選ぶ）・名前・
  持ち点（一括 + 席ごと）・1 ハンド目のボタン。検証（`build_session_config`）は端末の入力と同じ規則で、エラーはその項目の横に。
- **前回の設定を覚える**（`logs/last_session_setup.json`）。同じ画面にストラクチャーの編集（行の追加・並べ替え・ブレイク・
  複製・削除）と時計のタブ。
- 端末で `t` と Enter（または config `session_setup.enabled=false`、入力を流し込む起動 = 端末が対話でない）なら今までの端末の
  入力（先頭にモードの質問が 1 つ増えた: 空 Enter = リングゲーム / `t` = トーナメント）。

### D6. 卓モニタ

- 卓状態（`TableState.game` = `IntegrationThread.table_info()`）にモード・いまのハンドのブラインド・時計（`ends_at` などの
  **絶対時刻**）。モニタは PC の時計で残り時間・ブレイクまでを数える（毎秒の書き換え・履歴の増加をしない）。

## Consequences

- モードの既定はリングゲーム・アンテ無しで、起動の質問以外は今までと同じ記録（推定器の物差しは変わらない。内容の指紋は
  `integration/engine.py`・`core/poker_engine.py` などを変えたので動く）。
- 第 2 段（planned）: スタッフ画面のストラクチャーの編集（`GET/PUT /api/staff/blind-structures`）・セッション作成のモード・
  時計の操作（control queue に `clock`）・スタッフの `set_blinds` / アンテ・session の `game_type`。
- 第 3 段（planned）: 推定器 v2 の局面の特徴（`docs/estimator-v2/README.md` §3.3）に卓の種類を入れる。真のアクション入力の
  画面にレベル・アンテを出す。
- 5 月のブランチの GUI のタイマー表示（`gui/tournament_display.py`）・レイトレジストレーション・アドオンは移していない。

## 関連

- 作業ログ `docs/worklog/2026-10-24-table-modes-and-blind-structures.md`
- `docs/estimator-v2/README.md` §3.6 決定 5、台帳 `docs/plan-board.md`
- テスト `tests/test_blind_structure.py` / `tests/test_tournament_clock.py` / `tests/test_session_setup_ui.py` /
  `tests/test_table_state.py::TestGameInfo` / `tests/test_bb_ante.py`
