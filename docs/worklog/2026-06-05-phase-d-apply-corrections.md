# Worklog: Phase D part 1 — apply_corrections（合法手への射影）

## Date

2026-06-05

## Scope / Task

v1 リリーストラック R の Phase D（GitHub issue #7, R3 ルール準拠再構築）の **part 1**。
巨大フェーズ（15-20 人日）のため、まず pokerkit 非依存・完全テスト可能・ライブ未結線の純関数
`apply_corrections`（D1）を切り出して実装。統合ブランチ `v1-integration`、作業ブランチ
`claude/phaseD-reconstruction`。ISSUE-0009 の初期方針は Epic #4 で承認済み。

## Goal

- ADR-0009 §5 の修復表を `apply_corrections(action, amount, legal_ctx, whisper_conf) -> Correction`
  純関数として実装（`audio/recognizer.py`、ゲーム状態を持たない）。
- call/check を `amount_to_call` から決定的に一意化し、bet↔raise を状態から再マップ、amount を合法
  レンジへ snap、`needs_review` トリガを明文化。
- **ライブ挙動不変**（engine へ未結線）。pokerkit 不要で全テスト緑。

## Changed Files

- `audio/recognizer.py` — `Correction` データクラス + `apply_corrections()` + `_snap_to_legal()` を追加。
  `LegalContext` は `TYPE_CHECKING` import（`from __future__ import annotations` で実行時非依存）。
- `tests/test_phase_d_corrections.py`（新規, 18）— 修復表を網羅（pokerkit 不要）。
- `docs/issues/0009-...md` — 承認済み初期方針（silent-fold 上限/優先順位/needs_review）と D1 実装を記録。
- `docs/contracts/hand-reconstruction.md` — §5 に実装状況、§8 に check→call/fold の D1 暫定決定を追記。
- `CHANGELOG.md` — Phase D part 1 を追記。

## Expected Behavior

- check: `c==0`→check / `c>0`→call(+review, corrected_from=check)。`call` だが `c==0`→check(+review)。
- call: `c>0`→call 額は `c`（heard 無視）。
- bet/raise: 当ストリートのベット有無で bet↔raise を再マップ、amount を `[min_raise, max_raise]` に snap。
  amount 不明 / 大幅 snap / raise 非合法 は `needs_review`。
- allin→engine 再解釈用に `max_raise`(無ければ `amount_to_call`) を埋める。fold→そのまま。
- legal_actions 空（手番でない）→ passthrough + review。制御アクション（winner 等）→ passthrough。
- `whisper_conf` は結果（`asr_confidence`）へ持ち越すのみ（融合は D3）。

## Implemented Behavior

- 期待どおり。`Correction(action, amount, needs_review, corrected_from, reason, asr_confidence)` を返す。
- ベットに直面した "check" は **call + review**（プレイヤーを勝手に hand から外さない側、ISSUE-0009/§8 暫定）。
- blind 単位の round-number 寄せは LegalContext に blind が無いため未実装（clamp のみ）。

## Test Results

- `python -m pytest tests/test_phase_d_corrections.py -q` — **18 passed**。
- `python -m pytest tests/ -q --ignore=tests/test_vision.py` — **222 passed, 10 skipped**
  （従来 204 + 新規 18。skip は pokerkit 未導入分）。回帰なし（純関数・未結線のため挙動不変）。

## Mismatches Found During Testing

- None observed.

## Fixes Applied

- なし（新規実装）。

## Remaining Gaps / Out-of-Scope（Phase D の残り）

- [ ] **D0**: `PokerEngine` Protocol へ `legal_context`/`is_legal_actor`/`pots`/`committed`/`fold_through`
      追加 + `fold_through` 実装（legacy stub 含む）。
- [ ] **D2**: `integration/engine.py:328` の actor 推定結線（`resolve_actor` + silent-fold 合成、
      ISSUE-0009 承認方針）+ `apply_corrections` のライブ適用。
- [ ] **D3**: 派生 confidence（3 因子合成）+ `needs_review` 5 条件。重み較正は Phase F の golden fixtures。
- [ ] check→call/fold の尤度（chip-motion 等）導入（ISSUE-0009）。

## Related ADRs

- `docs/adr/0009-pokerkit-live-rules-authority.md`（§5 apply_corrections / §6 confidence）

## Related Issues

- `docs/issues/0009-actor-conflict-silent-fold-policy.md`（初期方針記録、Status Open のまま）
- GitHub issue #7（Phase D）/ Epic #4。

## Related Commits

- （本タスクの commit を後で追記）
