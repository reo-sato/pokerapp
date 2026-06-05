# ADR-0012: pokerkit を live 既定 backend に切替（Phase G）

## Status

Accepted（Phase G / #9 実装済。実機 E2E は Phase H で検証）

## Date

2026-06-05

## Context

ADR-0009 で pokerkit を rules-aware の権威 engine として導入したが、当初は **default-off**（live 既定は
素朴 round-robin の legacy `GameStateManager`）だった。その後 Phase D（R3: actor 推定 / `apply_corrections`
/ silent-fold 合成 / 派生 confidence）・Phase F（R4 決定的 replay / R5 golden fixtures + schema freeze）が
完了し、**pokerkit ルール準拠の再構築が golden fixtures 5 ケースで回帰固定**された（DoD #2）。

v1 の品質バーは「pokerkit ルール準拠の再構築を含める（精度重視）」。これを実際に live で効かせるには、
既定 backend を `legacy` → `pokerkit` に切り替える必要がある（ロードマップ Phase G）。本切替は最大の
回帰リスク（R-2: amount 意味の差・blind 自動 post・street 自動進行）を伴うため、前提と rollback を固める。

## Decision

1. **live 既定 backend を `pokerkit` に切替**: `config_default.json` の `engine.backend` を `pokerkit` に。
   新規インストール（config.json 不在 → `config_default.json` をコピー）は pokerkit を使う。
2. **legacy は rollback として残す**: `engine.backend="legacy"` を選べば従来 `GameStateManager`（挙動不変）。
   既存の config.json（legacy）はそのまま legacy を維持（破壊的変更にしない）。`create_game_state` の
   未知 backend fallback も legacy のまま。
3. **pokerkit を pin**: `requirements.txt` を `pokerkit>=0.7.0,<0.8.0` に。golden fixtures は pokerkit 0.7.x の
   pot/手番挙動に対して凍結しているため、minor 跨ぎの挙動差を防ぐ。
4. **切替の必要条件 = 全 golden 緑**（達成済）。ルール準拠の正しさは `tests/test_reconstruction.py` が担保。
5. **実機 E2E（音声→JSON/PHH）は Phase H で検証**（クリーン環境ビルド + 1 ハンド）。本 ADR の時点では
   unit/contract/golden で「既定が pokerkit」「legacy rollback 可」を固定する。

## Consequences

- **良**: v1 の精度の核（rules-aware 再構築）が既定で効く。actor 推定・合法手射影・silent-fold・side-pot・
  派生 confidence が live JSON/PHH に反映。
- **代償/リスク**: pokerkit が **hard dependency** になる（既に requirements にあり、今回 pin）。legacy と
  amount 意味等が異なるため、live 挙動が変わる（R-2）。→ legacy rollback + Phase H の実機 E2E で緩和。
- **既存ユーザー**: 既存 config.json は legacy のまま（明示更新 or 削除で pokerkit に移行）。`_comment` に明記。
- テストは明示 backend 構築のため既定切替の影響を受けず緑（286 passed）。

## Alternatives considered

- **default-off のまま維持**: v1 の「ルール準拠を含める」品質バーを満たせない。→ 却下。
- **legacy を完全削除**: rollback 手段と回帰比較の基準を失う。R-2 の安全網がなくなる。→ legacy を残す。
- **pin せず `>=0.5`**: 古い pokerkit で golden fixtures と挙動が乖離し得る。→ `>=0.7,<0.8` に pin。

## References

- ADR-0009（pokerkit rules authority, 当初 default-off）/ ADR-0010（record/replay）/ ADR-0011（replay harness）
- ロードマップ Phase G / リスク R-2、DoD #2/#5
- 実装: `config_default.json`, `requirements.txt`, `tests/test_phase_g_default.py`
- GitHub issue #9（Phase G）/ Epic #4
