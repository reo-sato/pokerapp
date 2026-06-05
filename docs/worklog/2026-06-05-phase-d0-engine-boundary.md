# Worklog: Phase D part 2 (D0) — engine 境界の rules-aware メソッド + fold_through

## Date

2026-06-05

## Scope / Task

v1 リリーストラック R の Phase D（GitHub issue #7）の **D0**。actor 推定（D2）が読む engine 境界を
整える: `PokerEngine` Protocol へ additive メソッド追加、`fold_through` 実装、`LegalContext` の中立化、
legacy stub。統合ブランチ `v1-integration`、作業ブランチ `claude/phaseD-actor-wiring`。

## Goal

- `PokerEngine` Protocol に `legal_context`/`is_legal_actor`/`pots`/`committed`/`fold_through` を追加。
- `PokerkitGameState.fold_through(until_seat)` を実装（silent-fold 合成）。
- legacy `GameStateManager` に rules-aware でない stub を追加（Protocol conform、ライブ挙動不変）。
- 循環 import を避けるため `LegalContext` を中立モジュールへ移し後方互換維持。

## Changed Files

- `core/engine_types.py`（新規）— `LegalContext` を移設（中立モジュール）。
- `core/poker_engine.py` — `LegalContext` を engine_types から再エクスポート、Protocol に 5 メソッド追加、
  `PokerkitGameState.fold_through()` 実装。`dataclass`/`Optional` の未使用 import を除去。
- `core/game_state.py` — legacy stub（`legal_context`=空 / `is_legal_actor` / `fold_through`=NotImplementedError /
  `pots`=[] / `committed`=0）。
- `audio/recognizer.py` — `LegalContext` の TYPE_CHECKING import を engine_types に変更。
- `tests/test_phase_d0_engine.py`（新規）— pokerkit 部（importorskip）+ legacy stub（常時）+ Protocol conform。

## Expected Behavior

- pokerkit: `fold_through(actor)`=no-op、中間席があれば silent fold 合成して actor を until_seat へ。
  未知席・非アクティブハンドは ValueError。`legal_context()` が actor/合法手/call 額を返す。
- legacy: `legal_context()` は空（rules-aware でない印）、`fold_through` は NotImplementedError。
- 両 backend が `isinstance(gs, PokerEngine)` を満たす（runtime_checkable）。
- ライブ挙動不変（D2 まで結線しない）。

## Implemented Behavior

- 期待どおり。`fold_through` は guard（テーブルサイズ超で ValueError）付き。
- `LegalContext` 移設は `core.poker_engine.LegalContext` / `core.engine_types.LegalContext` の双方で
  import 可能（既存 import・テストは無改修で通る）。

## Test Results

- `python -m pytest tests/test_phase_d0_engine.py tests/test_poker_engine.py tests/test_phase_d_corrections.py -q`
  — **40 passed**（pokerkit 0.7.4 導入済）。
- `python -m pytest tests/ -q --ignore=tests/test_vision.py` — **243 passed, 0 skipped**
  （pokerkit 導入で従来 skip の 10 件も実走）。未導入環境では pokerkit 部のみ skip（既存方針）。

## Mismatches Found During Testing

- None observed.

## Fixes Applied

- なし（新規実装 + 内部リファクタ）。

## Remaining Gaps / Out-of-Scope（Phase D の残り）

- [ ] **D2**: `integration/engine.py` の actor 推定結線（`resolve_actor` + `fold_through` 合成 + `apply_corrections`
      のライブ適用）。legacy は空 legal_context で従来経路に分岐。
- [ ] **D3**: 派生 confidence（3 因子）+ `needs_review` 5 条件。較正は Phase F の golden fixtures。

## Related ADRs

- `docs/adr/0009-pokerkit-live-rules-authority.md`（§2 Protocol / §4 fold_through）

## Related Issues

- `docs/issues/0009-actor-conflict-silent-fold-policy.md`（fold_through は §4 の合成機構）
- GitHub issue #7（Phase D）/ Epic #4。

## Related Commits

- （本タスクの commit を後で追記）
