# 2026-09-29 画面のコールの額をトータルにする（記録は追加額のまま）

## Goal

オーナー: 「UI上でコール額は追加額ではなく、トータルを表示してください」。記録（JSON）・真のアクション・計測の
`amount` はコールなら追加額（ベット・レイズ・オールインはトータル）で、これは変えない。画面に出すときだけ、
コールをその人がそのストリートで出した合計にする。

## Changed files

- `core/hand_log.py`: 表示用の計算 `street_flow`（各アクションの「前に出していた額」と「あとの合計」）/
  `street_totals` / `hand_street_flow` / `shown_amount` / `hand_stack_start`。合計 = ストリートの始めの持ち点 −
  `stack_after`（プリフロップはブラインド込み = `stack_start` は払う前）。持ち点が無い行は記録の額と前に出していた額
  から。訂正した行（`_original`, ADR-0036）は持ち点が訂正前のままなので、前に出していた額 + 訂正後の額。プリフロップの
  最初の行で前に出していた額（ブラインド）は、コール・チェック・フォールドなら持ち点から、ベット・レイズ・オールイン
  ならポジション（`position` / `position_map`、ヘッズアップはボタンが SB）から。
- `integration/engine.py`: `IntegrationThread.street_total(record)`（いまのハンドの記録から。integration スレッドで呼ぶ）。
- `main.py`（`--cli` の行）・`gui/dashboard.py`（`on_action` で額を決めて GUI スレッドへ渡す = GUI スレッドから engine を
  読まない）。
- `tools/ground_truth_ui.py`: 記録の行に `total`、`replay_legal` の各行に `total`・次の手番に `call_total`。画面は
  コール（とオールイン）をトータルで出し、「コール」ボタンも「コール 600」。入力する真のアクションの額は変えない。
- `shared/hand_replay/handReplayModel.ts`: `streetFlow`（Python と同じ計算）/ `streetTotals` / `shownAmount` /
  `recordedAmount`（訂正画面の金額欄 → 記録する額。コールは前に出していた額を引いて追加額へ）。`buildReplayModel` が
  各アクションに `total` を付ける。`HandReplay.tsx`・`handReplayText.ts` が `shownAmount` で出す。mobile / staff へ配布。
- 訂正画面（`mobile/src/screens/CorrectionScreen.tsx`・`staff/src/screens/HandCorrectionPanel.tsx`）: 一覧はトータル、
  金額欄もトータルで見せて入れ、保存で追加額へ戻す（前に出していた額より少なければ入力の誤りとして知らせる）。
- `api/static/player/`: お客さん向け画面を作り直した。

## Expected vs implemented

- 店舗 d0f055fb ハンド 8（ボタン 席4 レイズ 900 → SB コール → BB オールイン 5700 → 席4 コール → SB オールイン 13800 →
  席4 は残り 4800 でコール）: 画面は コール 900 / 5700 / 10500（前は 800 / 4800 / 4800）。
- BB が 600 のレイズにコール: 「コール 600」（前は 400）。フロップ以降は 0 から数える。
- 記録・真のアクションの JSON は変わらない（`amount` は追加額）。golden fixtures・店舗の fixture も変わらない。

## Test results

- `tests/test_call_totals.py`（新規 7: 持ち点からの合計・次のストリート・記録の額へのフォールバック・訂正した行・
  ヘッズアップ・壊れた行・ライブの `street_total`）、`tests/test_gui.py`（コールはトータル）、
  `tests/test_tools_ground_truth_ui.py`（記録の行の `total`・`call_total`・足りないオールイン）。
- TS: mobile 38 / staff 45 通過（`streetFlow` の訂正・ヘッズアップ・足りないオールイン・`recordedAmount`・テキスト共有）。
  mobile の型チェック通過。staff は src に型エラーなし（e2e は Playwright の型が無いだけ）。
- お客さん向け画面のビルド `--check` 通過。全体: 1956 passed / 5 skipped、ruff 通過。

## Remaining gaps

- `docs/usage.md` の画面の説明は、まとめて直すとき（CLAUDE.md §7b）に。
- 訂正は持ち点を直さない（ADR-0036 の重ね書き）ので、訂正した行のあとの同じ人の行は訂正前の持ち点から数える。

## Related commits

- （この worklog と同じ commit）
