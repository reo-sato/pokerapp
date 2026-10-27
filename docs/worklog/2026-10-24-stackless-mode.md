# 2026-10-24 持ち点を数えないモード

## Goal

オーナー: 「スタックサイズを無視して、オールインはオールインとしてだけ扱うモード」。確認したところ、10/23 に入れていたのは
採点の規則（`allin_equal` = オールインは種類だけで比べる）だけで、記録のモードは無かった。オーナーの決定:
1. オールインの額は記載しない。2. リングでも使う。

## 設計

- **ゲーム状態**（`core/poker_engine.PokerkitGameState(stackless=True)`）: 各席が `STACKLESS_STACK`（10^12）を持って毎ハンド
  始める（言った額を持ち点で切らない・持ち点 0 で配られない席が無い）。オールインは内部では最小のレイズ（仮の額）にして
  その席を `_allin_seats` に入れる。`_auto_act` が、オールインした席に回った手番を自動でコール / チェックし、オールインして
  いない人が手番の人だけで出す額が無ければチェックで進める（本物のオールインで pokerkit が手番を飛ばすのと同じ形）。
  `seats_to_act` からオールインした席を外す。手番の進め方はすべて `PokerkitGameState` の中に閉じた（engine は変えない）。
- **記録**（`core/stackless.py:strip_hand`、`HandSummary.to_dict` と進行中のハンドで呼ぶ）: 内部の仮の値を出さない。
  額が分からない = オールイン・そのストリートの最後の賭けがオールインのときのコール（`unknown_amounts`）。持ち点は常に null、
  ポット・収支・pot_awards の額はオールインのあったハンドだけ null。オールインの無いハンドの額・ポット・収支は正しい
  （始めの持ち点を毎ハンド同じ値にする = `stacks_before_next_hand`）。
- **設定**: ブラウザの設定画面のチェックボックス（`stackless`, 前回の値を覚える）。端末の入力は訊かずに config
  `engine.stackless`（訊くと入力を流し込む起動・テストの順がずれる）。卓の設定（`table.stackless`）として記録の先頭にも残す。
- **読む側**: 再生（`replay_events(stackless=)`）・推定器（`replay_world(stackless=)`、`setup["stackless"]` = 記録のハンドの
  `stackless` から `tools/eval_store.session_setup`）・真のアクション（`core/truth_hand.start_state` がハンドの `stackless` で同じ
  扱い、入力画面は持ち点の欄を出さない）・採点（額の無いコールは種類だけ）・PHH（このモードのハンドは飛ばす）・端末 / GUI の表示。

## Changed files

`core/poker_engine.py` / `core/stackless.py`（新規）/ `core/hand_log.py` / `core/truth_hand.py` / `integration/engine.py`（記録に
`stackless`・`amount_unknown`・進行中のハンド）/ `integration/replay.py` / `integration/world_replay.py` / `integration/estimator.py`
（`CONTENT_FILES` に `core/stackless.py`、持ち点の無いハンド）/ `tools/eval_store.py` / `tools/measure_capture_accuracy.py` /
`tools/ground_truth_ui.py` / `tools/session_setup_ui.py` / `main.py` / `gui/dashboard.py` / `config_default.json` /
schema hand 1.9・action 1.6 / `tests/test_stackless.py`（新規 12 件）。

## Tests

- `pytest -m "not slow" -n auto`: 3508 passed, 22 skipped。`ruff check .` 通過。画面の JavaScript は `node --check` で構文を確認。
- 開発データの 1 セッションを stackless として推定器にかけ、落ちずにオールインのハンドを推定できることを確認（手元のみ）。

## Remaining gaps

- 数えるモードとの切り替えはセッションの始めだけ（途中で変えない）。
- CLI の `r` / `d`（持ち点の買い足し・減らす）はこのモードでは意味を持たない（毎ハンド同じ持ち点で始める）。
- 推定器の値（オールインの事前など）はこのモードの店舗データでは測っていない。
