# 2026-09-30 全部正しいハンドの物差し + 推定を記録に重ねる読み手

## Goal

- オーナー: 「結局ハンド全体が整合してることが重要なので、打率 9 割の発話解釈だとハンドの整合率自体は全然物足りない」。
  提案 1（推定器を記録の本体に）・2（目標を全部正しいハンドの割合に）を承認、3・4 は保留。
- 目標の数字を 1 つのコマンドで出す。推定を記録の本体にするための読み手の側（重ね方・訂正の当て方）を先に作る
  （推定器 v1 そのものは監査のあと）。

## Changed files

- `tools/measure_capture_accuracy.py`: `hand_fully_correct`（行が全部正しい・余計な行なし・勝者・ボード）。
- `tools/bench_hands.py`（新規）: 店舗 / 台本 / シミュレーションごとの全部正しいハンド（推定器なし・ありの対、
  正解が候補に入った数、良くなった・悪くなった・崩れたままのハンド）。`--quick` / `--no-sim` / `--json`。
- `tools/estimate.py`: 全部正しいハンドを数える。声だけの台本のセッション（`tests/fixtures/script`）を推定器の入力に
  （`script_input`: 台本の画面を押した時刻でハンドを始め、発話は話し始めの時刻に置く。やり直したハンドは後の回）。
- `tools/eval_store.py`: 全部正しいハンドを出す。
- `core/hand_estimate.py`（新規）: `<sid>.estimate.json`（hand_id → 推定）をライブの記録に重ねる
  （ADR-0056 D1 = live ⊕ estimate ⊕ staff corrections）。記録の識別・時刻・札の読み取り・席の人はライブ、
  アクション・勝者・ポット・結果・ボタンは推定。`estimate`（版・差・事後確率・次点）と `_live` を付ける。
  始まりの時刻が合わない推定は使わない。
- `api/read_models.py` / `tools/ground_truth_ui.py` / `main.py --export-phh`: 記録を読むところで重ねる。
- `core/hand_correction.py` / `hand_correction_repository.py` / `api/server.py`: 訂正に `target`（訂正の前の
  street・seat・action・amount）を残し、行番号の行が違えば同じ行を探して当てる（1 つに決まらなければ当てない =
  `_corrections_skipped`）。`target` の無いこれまでの訂正は行番号。
- `tests/test_bench_hands.py`・`tests/test_hand_estimate.py`（新規）。

## Expected vs implemented

- 物差し: 期待どおり。店舗 12/18（67%）→ 推定 v0 13/18、台本 27/30 → 27/30、シミュレーション 57/120 → 62/120。
- 重ね方: ロガーはまだ推定のファイルを書かない（`tools/estimate.py --write` の形は重ね方の形と違い、読まれない）ので
  いまの動きは変わらない。切り替えは推定器 v1 と監査のあと。

## Test results

- `pytest tests/test_bench_hands.py tests/test_hand_estimate.py` 23 passed。全体は下の commit の時点で green。

## Mismatches / remaining gaps

- 店舗 027e4b15 ハンド 1 は真のアクションにショーダウンのマックの行が無い（入力の決めごとの差）ため丸ごと正しくならない。
- ロガーがハンドの終わりごとに推定して `<sid>.estimate.json` を書く部分は未着手（推定器 v1 の形が決まってから）。
- 崩れた 8 ハンドの直し方の確認は `docs/worklog/2026-09-30-estimator-v1-design.md`。

## Related commits

- （この worklog の commit）
