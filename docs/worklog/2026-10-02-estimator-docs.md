# 2026-10-02 推定器の設計と監査への対応を 1 つの文書にまとめる

## Goal

オーナー: 「推定器に関する設計計画と監査の内容ってドキュメントに残ってる？」→ 残っているが、(1) 10/01 の推定器の変更は
作業ログの節に散らばり、v1 の設計の作業ログは 9/30 のまま、(2) 10/01 の決定（評価の前に音声入力を改善）と作業計画の
監査の結論は ADR-0056・decision-log に未反映（店舗テスト中の軽い運用 §7b）、(3) 監査の推奨ごとの「済み / 未着手」は
10/01 の監査の対応表で止まっている、と答え、オーナーの「お願いします」でまとめ直した。

## やったこと

- `docs/estimator.md`（新規）: いまの設計を 1 か所に。9/30 の設計の作業ログは監査が見た版なので書き換えず、冒頭に案内だけ
  足した（監査の原文の節の参照が崩れないように）。
  - 採点の項と値はコードから写した（`integration/estimator.py` の `PARAMS`・`tools/estimate.py` の `PARAMS` と
    `READING_OVERRIDES`）。いまの `params_hash` は `46239ad17aad`（固定した `5e54104723c3` からの変更は 15edff5 の
    `amount_prior_weight` と fb64fcb の `ear_amount_top`。5587567 は読みの入力を変えたが指紋は同じ）。
  - 監査の推奨ごとの状態は、10/01 の監査の対応表を起点に、そのあとの変更（額の空間のステップ 1・2）をコードで確かめて
    付け直した。確かめたこと: 推定器は「終わったあとの勝者の指定の食い違い」を理由にしていない（未）/ 推定器まわりの
    ファイルの内容の指紋は無い（未）/ 物差しに除外規則は無い（未）/ 第 2 の耳ありの意味のない語の額の既定は救い出しと
    同じ −0.5 のまま（一部）/ 別名「コーナー」などは足していない。
- ADR-0056 に追記 3（10/01 の決定・承認と作業計画の監査の結論、オーナーの判断待ち 6 点）と Related に文書の地図。
- decision-log の ADR-0056 の行、CLAUDE.md（ADR-0056 の注記・実装状況の表に推定器の行・docs の構成・よく使うコマンド）、
  `integration/estimator.py` の冒頭の説明（設計の文書の場所）、CHANGELOG。

## Changed files

`docs/estimator.md`（新規）/ `docs/adr/0056-post-hoc-probabilistic-action-history.md` / `docs/decision-log.md` /
`CLAUDE.md` / `CHANGELOG.md` / `docs/worklog/2026-09-30-estimator-v1-design.md`（冒頭の案内だけ）/
`integration/estimator.py`（説明の文だけ）/ この作業ログ

## Test results

コードの変更は説明の文だけ。`tests/test_estimator.py` を流して確かめた。

## Remaining

- `docs/estimator.md` は推定器を変えるたびに直す（§7 の表・§6 の変更の一覧・`params_hash`）。
- 事前登録は保留のまま（固定し直すときに新しい版として書き直す）。
