# Worklog: Rules-aware hand reconstruction & contract-first hand core (設計提案 R0)

## Date

2026-06-03

## Scope / Task

hand logger の **目的（ノイジーな ASR＋RFID からの正確な再構築）と思想（contract-first / fixtures-as-oracle）**
を、よりよく実現するための方策・アルゴリズム・アプリ設計を **設計提案ドキュメント**として残す（docs-only,
実装は後続フェーズ）。重点: (1) 再構築精度（アルゴリズム）/ (2) 思想・アーキテクチャ。

## Goal

- 現状の核心ギャップ（actor 推定欠如 / ルールエンジン不在 / ASR の文脈非依存 / 静的融合 / hand core が
  contract 圏外）を一貫して解く設計を、ADR＋contract doc＋issue として凍結する。
- 既存 `logs/*.json` / PHH を壊さず段階導入できる道筋（R1..R5, additive・rollback 安全）を示す。
- "done" = code/schema/fixtures を変えずに、提案文書一式 + traceability（decision-log / CHANGELOG /
  本 worklog / CLAUDE.md）が揃い、相互参照が解決する。

## Changed Files

新規（docs のみ）:

- `docs/adr/0009-pokerkit-live-rules-authority.md` — pokerkit.State を live ルール権威に（engine/dependency 決定）。
- `docs/adr/0010-rules-constrained-estimation-and-fusion.md` — actor 推定 / `apply_corrections` / 派生 confidence。
- `docs/adr/0011-contract-first-hand-core-record-replay.md` — hand core の contract 化と決定的 record/replay。
- `docs/contracts/hand-reconstruction.md` — engine/estimator 設計詳細（`PokerEngine` interface / 修復表 /
  `hand`・`action` inline sketch）。
- `docs/contracts/event-replay.md` — record/replay harness / 決定性 / `reconstruction_event` sketch / golden fixtures。
- `docs/issues/0008-pokerkit-online-feeding-feasibility.md`（ADR-0009 の gate）。
- `docs/issues/0009-actor-conflict-silent-fold-policy.md`。
- `docs/issues/0010-replay-determinism-record-boundary.md`。
- `docs/issues/0011-hand-action-schema-freeze-blockers.md`。

更新（docs のみ）:

- `docs/decision-log.md` — ADR-0009/0010/0011、ISSUE-0008..0011 を index 登録。
- `CHANGELOG.md` — `Unreleased` に Phase R0 設計提案セクションを追加。
- `CLAUDE.md` — 実装状況表の「actor 推定」「数値正規化」行に設計提案リンクを付与（❌ 未実装は据え置き）、
  Phase candidates に R0–R5（hand core 改善トラック, 提案）行を追加。

**`.py` / `schemas/*.json` / `fixtures/` は一切変更していない**（planning-only ガード）。

## Expected Behavior

- 提案文書が本リポジトリの doc 規約（ADR/issue テンプレ節構成、contract doc 構造、decision-log index）に
  一致し、ADR↔contract↔issue↔decision-log の相互リンクが解決する。
- 未実装機能が planned/proposed として明示され、「実装済」と誤記しない。
- 既存コード・テスト・既存契約（player/session schema・fixtures）に影響を与えない。

## Implemented Behavior

- 3 ADR（Proposed）+ 2 contract draft + 4 issue（Open）+ traceability 更新を追加。設計の核心:
  - **ADR-0009**: 現 `GameStateManager` の安定 I/F 背後で `pokerkit.State` へ差し替え（`engine.backend` フラグ）、
    raw ASR を直接流さず合法手へ射影する「境界での推定」、出力 additive。`pokerkit` が宣言済み・**未 import**
    である事実と `game_state.py` の Phase 3 TODO を正確に引用。
  - **ADR-0010**: actor 推定（手番 prior × RFID/audio/camera + **silent-fold 自動合成**）、`apply_corrections`
    （合法手制約・**call/check を amount_to_call から一意化**・amount スナップ）、派生 confidence
    （`L×(A,Q)`, 固定テーブル置換）と `needs_review` 条件の明文化。
  - **ADR-0011**: `hand`/`action`/`reconstruction_event` の contract 化、append-only event sidecar、
    **注入クロック**による決定的 replay、golden fixtures を core の oracle に。ADR-0008 と整合。

## Test Results

- **pytest は本リモート実行環境に未インストール**（`python -m pytest` → "No module named pytest"）。
  本タスクは **docs-only**（`.py` / schema / fixtures 不変）であり、テストスイートの挙動には影響しない。
  pytest 導入環境での baseline 確認コマンド: `pytest tests/ -q --ignore=tests/test_vision.py`（変更前後で
  同一結果を期待）。
- doc 整合（手動 grep, 緑）:
  - `grep -rln "ADR-0009|ADR-0010|ADR-0011" docs/` / `"ISSUE-0008..0011"` → 新規 ADR/issue/contract doc と
    `decision-log.md` で相互参照が解決。
  - 新規 9 ファイル（3 ADR + 2 contract + 4 issue）の存在を確認。

## Mismatches Found During Testing

- None observed（docs-only。整合 grep は期待どおり解決）。
- 留意: pytest 不在のため自動回帰は本環境で未実行。コード未変更につき regression 余地はないが、実装フェーズ
  （R1 以降）では pytest 環境で baseline を取り直す。

## Fixes Applied

- なし（新規文書作成のみ）。

## Remaining Gaps / Out-of-Scope

- [ ] **ISSUE-0008**（pokerkit online API 実現性, ADR-0009 の gate / 本環境では pokerkit 未インストールで未検証）。
- [ ] **ISSUE-0009**（actor 競合・silent-fold ポリシー）/ **ISSUE-0010**（replay 決定性の記録境界）/
      **ISSUE-0011**（hand/action freeze）。
- [ ] 実 schema ファイル化・fixtures・`tests/` 配線（R4）、実装（R1..R5）は別タスク。
- Out-of-scope（今回重点外）: 堅牢性（VAD/スレッド安全/クラッシュ復旧）、出力忠実度の出力表現（PHH hole card 等）。
  call/check・side-pot の PHH 改善は R3/R5 の下流効果として doc に明記。

## Related ADRs

- `docs/adr/0009-pokerkit-live-rules-authority.md`
- `docs/adr/0010-rules-constrained-estimation-and-fusion.md`
- `docs/adr/0011-contract-first-hand-core-record-replay.md`
- 関連: `docs/adr/0008-hand-logger-session-integration-strategy.md`（additive / immutability / sidecar 前例）

## Related Issues

- `docs/issues/0008-pokerkit-online-feeding-feasibility.md`
- `docs/issues/0009-actor-conflict-silent-fold-policy.md`
- `docs/issues/0010-replay-determinism-record-boundary.md`
- `docs/issues/0011-hand-action-schema-freeze-blockers.md`

## Related Commits

- 本 worklog と同じコミット（reconstruction engine design planning, code 未変更）
