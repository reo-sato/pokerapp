# Worklog: Phase A 捕捉精度計測ハーネス（measurement-plan + tool + tests）

## Date

2026-06-26

## Scope / Task

ハンドレビュー × GTO solver 統合提案 rev.1（`docs/proposals/2026-06-26-hand-review-integration.md`
§6）が前提条件にしている **Phase A（捕捉精度 95%）** を客観的に判定可能にするための
計測プラン（A1）と計測ツール（A2）を実装する。

調査（`/root/.claude/plans/dapper-gathering-hammock.md` の Phase A 状況確認結果）で:

- Phase A の合否定義が文書上に存在しない（「95% 以上」だけ）
- 計測ハーネスが存在しない
- ground truth 形式が定義されていない
- Phase H（実機 E2E）は別 repo 依存で着手待ち

の 4 点が判明していた。本 PR で前 3 つを解決し、M1 着手の Go/No-Go 判定基盤を作る。
Phase H は別 repo 依存のため対象外。

## Goal

- 「95% 以上」の **分母と分子** を文書で確定（3 軸: hand_coverage / action_accuracy /
  board_accuracy、すべて ≥ 95% を必要条件とする）
- ground truth ファイル形式を確定（`logs/{session_id}.ground_truth.json`、append-only、
  schema を docs に明記）
- session log × ground truth × ハンド訂正（ADR-0036）を突き合わせて 4 軸 + 診断指標を
  算出する CLI ツール
- 訂正は **既定で適用**（ユーザー可視の最終状態を測る）。`--raw` で素地も測れる
- exit code で pass/fail を返し CI / shell スクリプトから扱える
- 完成時点で **20 件の単体テスト + CLI smoke** が緑

## Changed Files

- `docs/dogfood/measurement-plan.md` — 新規（rev.1）。Phase A 合否 3 軸 / 診断軸 /
  ground truth schema / dogfood 規模 N=5〜10 × 8 週 / Phase B KPI 暫定閾値の正式化
- `tools/measure_capture_accuracy.py` — 新規。`measure_session()` / `measure_hand()` /
  `load_corrections_for_session()` + CLI（`--session` / `--ground-truth` / `--corrections` /
  `--raw` / `--json` / `--threshold`）。exit code 0 = pass、1 = fail、2 = input error
- `tests/test_measure_capture_accuracy.py` — 新規。20 件のテスト（perfect match / missed /
  phantom / action 種別-金額分離 / max() 分母 / board 順序非敏感 / hole_cards=None / 訂正適用 /
  pass-gate 3 軸 / CLI smoke 3 件）
- `CLAUDE.md` — 「よく使うコマンド」に `python tools/measure_capture_accuracy.py ...` を追加
- `CHANGELOG.md` — Unreleased に新節 3 つを追加（measurement harness / ADR drafts /
  proposal rev.1。本 PR と直近 2 commits の差分をまとめて Unreleased 化）

## Expected Behavior

- `tools/measure_capture_accuracy.py --session X --ground-truth Y --raw --json` が
  3 軸 + 診断軸を JSON で出力し、3 軸すべて ≥ 0.95 なら exit 0
- 訂正なしで `--raw` を付けると素地（訂正されていない誤認識）が見える
- ground truth が空 / セッション log が空 / 該当 session_id の訂正がない場合も crash しない
- 完全一致のテストで 4 軸すべて 100%、`phase_a_pass = True`
- 1 ハンドだけ missed の状態で `hand_coverage = 0.5`、`phase_a_pass = False`
- 訂正で action を直すと、訂正なしでは 0.5 だった action_accuracy が訂正適用で 1.0 になる

## Implemented Behavior

期待通り。具体的に確認した挙動:

- `pytest tests/test_measure_capture_accuracy.py -v` → **20 passed**（0.27s）
- `python tools/measure_capture_accuracy.py --help` → 引数 6 つの説明が日本語で表示
- CLI smoke `test_cli_runs_and_exit_code_reflects_pass` → 完全一致で exit 0、JSON に
  `"phase_a_pass": true`
- CLI smoke `test_cli_returns_1_when_below_threshold` → 1 hand missed で exit 1、human
  出力に `FAIL` を含む
- CLI smoke `test_cli_input_error_returns_2` → 入力ファイル不在で exit 2

実装上の判断（プラン時点の意図と一致）:

- `action_total = max(len(gt_actions), len(captured_actions))` で **抜けと過剰の両方** を罰する
- fold / check は amount を 0 に正規化（amount の誤差で罰しない）
- board は順序非敏感（sort して比較）
- ground truth に `hole_cards: null` の seat は分母から除外（preflop fold で hero unknown を
  罰しないため）
- 訂正は `apply_hand_corrections` 既存実装を再利用（ADR-0036、コード重複なし）

## Test Results

- `pytest tests/test_measure_capture_accuracy.py -v` — **20 passed in 0.27s**
- `pytest tests/test_measure_capture_accuracy.py tests/test_hand_correction.py
  tests/test_atomic_io.py tests/test_backup.py -q` — **40 passed in 0.31s**（既存テストへの
  副作用なし）
- 注: 本環境では numpy / 他の audio/RFID 系 dep が未インストールのため、`pytest tests/ -q` は
  collection error で止まる（環境問題、本 PR と無関係）。私の追加分は pure-stdlib + core 依存のみ
- `python tools/measure_capture_accuracy.py --help` → 期待通り

## Mismatches Found

なし。プラン時点の意図と実装挙動が一致。

## Remaining Gaps

本 PR の範囲外で残るもの:

1. **ground truth 入力 UX**（A3）— スタッフが iPad app から手動入力するか、別画面で記録するか、
   表計算からインポートするかは別議題。本 PR は **ファイル形式の確定** までで、入力 UI は別タスク
2. **Phase H（実機 E2E）** — 別 repo（RFID firmware）依存。本 PR では着手できない
3. **CI 統合** — 本ツールを CI 上で「ground truth が増えるたびに自動計測」する仕組みは
   dogfood 開始後の運用設計マター
4. **複数セッション横断の集計** — 本ツールは 1 セッションずつ計測する設計。週次レポートは
   別ツール or シェルスクリプトで合算する想定（dogfood 運用時に追加）
5. **M1（solver 統合）の着手** — Phase A 3 軸が ≥ 95% に達してから

## Related Commits

このタスク完了時の commit に記載。

## Related ADRs / Issues / Reviews

- 起票元提案: `docs/proposals/2026-06-26-hand-review-integration.md`（rev.1 §6）
- 計測の根拠: `docs/dogfood/measurement-plan.md`（rev.1, 本 PR で新規）
- 訂正適用: ADR-0036 (hand correction overlay)
- v1.0 ローンチレビューの Phase A/B/C 構造: `docs/reviews/2026-06-15-v1.0-launch-review.md`
- M1 解禁後の関連 ADR: ADR-0040 / 0041 / 0042（前 commit で起票済、すべて Proposed）
