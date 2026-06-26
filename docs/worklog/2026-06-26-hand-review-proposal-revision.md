# Worklog: Hand Review × GTO Solver 統合提案ドキュメントの新規配置（rev.1）

## Date

2026-06-26

## Scope / Task

ハンドレビュー機能（TexasSolver 統合）の提案ドキュメントを `docs/proposals/` に新規配置する。
コードベース照合レビュー（`/root/.claude/plans/dapper-gathering-hammock.md`）の Must 6 件 +
Should 4 件を反映した rev.1 を初稿として置く。**M1 実装着手前のドキュメント整備のみ**。

## Goal

- `docs/proposals/2026-06-26-hand-review-integration.md` を新規作成（提案そのものは
  会話上のテキストとして存在していたがファイル化されていなかった）
- レビュー Must 項目を反映: B4 訂正接続 / 既存モジュール名訂正 / `gui/` 規約整合 /
  データ実体ギャップ明示 / ADR 3 件起票約束 / Pio 差分の M1 前倒し
- レビュー Should 項目を反映: KPI 暫定閾値内包 / dogfood 規模明示 /
  R5 自社ソルバー削除 / R2 を 10 アーキタイプに精緻化
- 「Phase A 95% 未通過 / 状況不明」が確定したため、M1 実装着手は本ドキュメント上で
  明示的に前提条件化（§6 表 + §2 文言）

## Changed Files

- `docs/proposals/2026-06-26-hand-review-integration.md` — 新規作成（rev.1）

## Expected Behavior

- ファイルとして提案が読める状態になる
- レビュー時に指摘された silent failure 源（訂正前ハンドへの GTO 重ね表示）が
  本提案の非目的セクション (§3) に明示される
- 新規モジュール配置が `gui/` 既存規約と整合
- ADR 3 件（仮 ID 0040〜0042）の起票約束が ADR commitments 節 (§4.6) に明示
- Phase B の合否基準が暫定値とはいえ本提案単体で読める (§6.2)

## Implemented Behavior

期待通り反映済み。差分はなし。具体的な反映箇所:

- §2 / §4.1 / §4.4: 入力源を `api/read_models.py:get_hand()`（B4 訂正適用済）に固定
- §3 末尾: 「B4 ハンド訂正済みでないハンドへの GTO 重ね表示」を非目的に追加
- §4.1 表 + §4.1 「入力前のフィルタ規約」: ポジション未保持 / hole_cards null /
  pots backend 依存の 3 ギャップを明示
- §4.2: `ui/review/single_hand_review.py` → `gui/hand_review.py`。`main.py --review` 起動パターン
- §4.6 新設: ADR-0040 (SQLite cache) / ADR-0041 (vendored binary) / ADR-0042 (subprocess) の
  仮 ID と論点
- §6.1 新設: dogfood N=5〜10, X=8 週
- §6.2 新設: KPI 暫定閾値（週次起動率 / 滞在時間 / 能動操作率 / ベースライン質問）+
  計装の置き場所（`logs/hand_review_usage.jsonl`）
- §7 / §R4: Pio 差分 3 アーキタイプ手動 QA を M1 内へ前倒し
- §R2: 10 ハンド → 10 アーキタイプ（具体リスト、`tests/fixtures/solver_roundtrip_*.json` への固定）
- §R5: 「自社ソルバー実装」フォールバック削除（複数エンジニア年規模で非現実的）
- §1.1 新設: 自店ユーザー層と「上達志向プレーヤー」の適合性チェック
- §12 新設: 改訂履歴

## Test Results

ドキュメントのみの変更のため自動テストは無し。

- `git status` — `docs/proposals/2026-06-26-hand-review-integration.md` (new) +
  `docs/worklog/2026-06-26-hand-review-proposal-revision.md` (new) のみ
- レンダリング確認: markdown 構文・テーブル整形を目視

## Mismatches Found

なし。レビュー Must/Should を 1:1 で反映。

## Remaining Gaps

本提案を **rev.1** として配置したが、以下は別タスク:

1. **ADR-0040/0041/0042 の起票** — 提案 §4.6 で約束したのみ。実起票は M1 着手前に行う別ブランチ/PR
2. **`docs/dogfood/measurement-plan.md` の作成** — 提案 §6 が参照する別文書。本 PR 範囲外
3. **`docs/decision-log.md` への ADR Index 追加** — ADR 起票時に同時更新（本 PR ではまだ）
4. **CLAUDE.md 更新** — 本機能は未実装のため「Future Scope」に項目を増やすかは
   別判断。現状では rev.1 提案を読めば足りる
5. **CHANGELOG.md 更新** — 未実装機能のため Unreleased 追記は M1 着手時にまとめて
6. **Phase A 通過確認** — 提案 §6 の前提条件。現時点で **未通過 / 状況不明**。
   M1 着手は Phase A の捕捉精度検証が 95% 以上で通過した後

## Related Commits

このタスク完了時の commit に記載。

## Related ADRs / Issues / Reviews

- レビュー本体: `/root/.claude/plans/dapper-gathering-hammock.md`（rev.1 反映元）
- ADR-0036 — Hand correction overlay（提案 §4.1 入力源の根拠）
- ADR-0011 — Deterministic replay harness（提案 §2 入力源の生成系）
- ADR-0033 — Derived confidence weight calibration（R2 / R4 品質保証パターンの参照先）
- `docs/reviews/2026-06-15-v1.0-launch-review.md` — 提案 §1 背景
