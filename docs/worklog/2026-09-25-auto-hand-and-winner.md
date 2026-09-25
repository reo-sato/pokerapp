# Worklog: 手札が配られたらハンドを始め、勝者を自動で決める（ADR-0062 / ISSUE-0038）

## Date

2026-09-25

## Scope / Task

店舗の通しテストでハンドが一度も始まらなかった（ISSUE-0038）。オーナーの選択:

- ハンドの始まり = 手札が配られたら自動
- ショーダウンの勝者 = 手札とボードから自動判定
- 金額 = 言った数字どおり（「ナナ」= 7）

途中で追加の指示が 2 つあった:

1. 「ベッティングが終わった直後のフォールドは、一番アウトオブポジションから順にフォールドさせ、残った人を勝者に」
2. 「実際には勝っているハンドをアウトオブポジションが見せずにマックすることがある。その場合に自動判定で勝者を誤って記録しては
   いけない」

→ 2 を受けて、ショーダウンでは**マックの宣言を手札より優先**し、手札で判定するのは誰もマックしなかったときだけにした。

## Goal

`n` も席番号も言わずにハンドが回り、勝者が正しく記録される（見せずにマックした人は手札が強くても負け）。

## Changed Files

- `integration/engine.py`
  - 配布の検出: `_is_next_deal` / `_collect_deal` / `_start_dealt_hand_if_ready` / `_adopt_deal_cards` / `_close_hand_for_next_deal`。
  - 勝者: `_maybe_finish_hand` / `_finish_by_rules` / `_finish_showdown` / `_apply_showdown_muck` / `_mucked_stronger_hand` /
    `_handle_end_hand` / `_late_winner`。
  - `_finalize_hand`: awards / winner_source / showdown / ended_ts。お知らせ `on_notice`。
- `core/showdown.py`（新規）— `evaluate_hands` / `award_pots`（pokerkit の役判定）。
- `core/poker_engine.py` — `rules_aware` / `end_hand_awards` / `acting_order` / `current_pots`（Protocol にも追加）。
  `core/game_state.py` — legacy の最小実装。
- `core/hand_log.py` — `winner_source` / `showdown`。
- `audio/recognizer.py` — 語の直後の仮名の数（`_kana_number_at` / `_kana_amount_to_kanji`）。
- `core/constants.py` — 「ベト」「ゴール」「ハンド終了」。
- `main.py` — `_auto_hand_kwargs` / `_print_notice`、CLI と GUI に結線。起動時の案内。
- `integration/replay.py` — `auto_new_hand` / `auto_winner`（setup.json でも指定可、既定 false）。
- `config_default.json` — `engine.auto_new_hand` / `engine.auto_winner`（既定 true。config に無い既存の PC も有効）。
- schema: `hand` 1.4 / `action` 1.5 / `versioning-and-freeze.md`。
- tests: `tests/test_auto_hand.py`（新規 51）。
- docs: ADR-0062 / ISSUE-0038 / 本 worklog / `docs/usage.md` / `docs/troubleshooting.md` / CLAUDE.md / CHANGELOG / decision-log。

## Expected Behavior

- 2 席以上に札が配られたら「ハンド N 開始（手札が配られました / ボタン 席X）」。配った札はそのハンドの手札。
- 全員フォールドで即確定。ショーダウンでは「フォールド」= OOP から順のマック。誰もマックしなければ「ハンド終了」か次の配布で
  手札から判定（side pot・引き分けも）。
- 配る前に話したアクションは、認識が遅れても前のハンドに入る。
- 「ベトナナ」= bet 7、「レイズ ゴール」= raise / call。

## Implemented Behavior

- 期待どおり（テストで固定）。実機での確認は店舗で行う。

## Mismatches

- 最初はベッティングが終わった時点で手札を優先して判定する案だった。オーナーの指摘（見せずのマック）で、マックを優先し、
  手札は誰もマックしなかったときだけに変えた。
- 3 人以上のショーダウンで、順番どおりにマックしたとは限らない → `muck_order_assumed` で要確認。
- ベッティングが終わったあとの `check` / `call` は、従来の `no_active_hand`（「n を打って」）ではなく `betting_over` にした。

## Test Results

- `tests/test_auto_hand.py` 51 passed。
- 全体: `pytest tests/ --ignore=tests/test_vision.py` 1418 passed / 5 skipped（pwsh の無い環境のインストーラ検査）、
  `ruff check .` clean。

## Remaining Gaps

- 店舗での確認（配布で始まる / 片付けで早まらない / マックとハンド終了 / 次の配布での確定）。
- 手札が読めていない席に次のハンドの最初の札が来ると、前のハンドの手札として拾うことがある。
- replay は発話の認識待ちを持たない（`replay_events(auto_new_hand=True, auto_winner=True)` / setup.json で有効化）。

## Related Commits

- （本 worklog と同じコミット）
