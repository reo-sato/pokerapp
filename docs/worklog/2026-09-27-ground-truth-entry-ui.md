# 2026-09-27 真のアクション入力の画面（通しでプレイして真の履歴を蓄積する）

## Goal

オーナーの提案（2026-09-26）: 1 ハンドずつ見て直すのではなく、通しで何ハンドもプレイして、ハンドごとに
**実際に起きたアクション列**（ground truth）を入れ、データを蓄積する。そのための入力画面を作る。

店舗 PC で動くのは `--cli` + 卓モニタ + お客さん向け画面（Node なし）なので、staff iPad アプリの計測タブ
（ADR-0043）は使えない。卓モニタと同じ仕組み（標準ライブラリの HTTP サーバ + 1 枚の HTML）で、iPad / スマホ /
PC のブラウザから使える画面にした。

## Changed files

- `tools/ground_truth_ui.py`（新規）: `logs/<sid>.json` を読み、`logs/<sid>.ground_truth.json` を
  `GroundTruthRepository` で書く。API は `GET /api/sessions` / `GET .../hands` / `GET .../hands/{hid}` /
  `PUT .../hands/{hid}`（passthrough / manual-edit）/ `POST .../hands/{hid}/legal`（pokerkit で手番・ストリート・
  コールの額を補う）。ハンド訂正（`hand_corrections.json`）があれば重ねて表示・比較する。
- `start_truth.cmd`（新規, ASCII/CRLF, `--host 0.0.0.0 --port 8791`）/ `installer/install.ps1` のショートカット
  「真のアクション入力 (iPad から)」/ `tests/test_installer.py`。
- `tests/test_tools_ground_truth_ui.py`（新規 19 件）。
- `docs/usage.md`（「真のアクション履歴を入れる」節）/ `CHANGELOG.md`。

## Expected vs implemented

- 一覧: セッションを選ぶ（既定 = 最新）。ハンドは新しい順、要確認 / 未入力・記録どおり・修正済 / 一致・差分の印。
  5 秒ごとに読み直す（卓の脇でハンドが終わるたびに入れられる）。上に入力済みハンドの一致率
  （ハンド / アクション / ボード / 勝者）。「次の未入力 →」。
- 編集: 左に記録（聞き取った文・要確認の理由つき）、右に実際。ボードと手札はカードをタップして選ぶ
  （使用中のカードは薄く）。アクションは「次: 席 N の番」のボタン（フォールド / チェック・コール N / ベット・
  レイズ + 額 / オールイン）で順に足す。行の席・アクション・額は直せて、直すたびに pokerkit で流し直す
  （手番違い・額の範囲外は行を赤くして理由を出す。チェックできるときのフォールドは `force_fold` で通す）。
  全員が降りれば勝者は自動、ショーダウンなら席を選ぶ。
- 保存: 「✓ 記録どおり」= captured-passthrough（要確認のハンドは拒む = staff API と同じ C-2 ガード）/
  「保存」= manual-edit（形を検査: カード表記・重複・アクション種別）。保存の応答に記録との一致 / 差分
  （`measure_hand` と同じ計算）。
- GT の額の規約は記録と同じ（ベット / レイズ = トータル、コール = 足した額、フォールド / チェック = 0）。
  コールの額は pokerkit の `amount_to_call` で自動なので、入力者は考えなくてよい。

## Tests

- 新規 19 件（読み取り 5 / 保存 6 / 手番補完 6 / 検査 2）+ installer 3 件更新。全体 1653 passed / 5 skipped、ruff clean。

## Remaining gaps

- 記録に `players` / `blinds` が無い古いログでは手番の自動補完が使えない（行ごとにストリートを選ぶ退避経路あり）。
- 生の在否（席の札の在る / 無い）の記録はまだ無い。離脱の判断そのものを直したあとに古いセッションで確かめるには
  必要（次のタスク）。複数セッションの集計と差分の分類も未着手。
- チョップ（複数勝者）は `winner_seat` 1 つ + メモで表す。
- 通しの店舗テストで実際に使ってから、入力の手間（1 ハンドあたりの時間）を見て手直しする。
