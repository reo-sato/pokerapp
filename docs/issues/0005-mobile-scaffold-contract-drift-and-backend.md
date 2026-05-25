# Issue 0005: mobile scaffold の contract drift / backend integration / offline sync

## Date

2026-05-25

## Status

Open

## Severity / Priority

- Severity: Medium
- Priority: P2

## Area

mobile (WS3) / contracts / future-scope (S5)

## Expected Behavior

mobile front-end (WS3) は core (WS1) と同じ shared contract（`docs/contracts/`）に
従う consumer であり、business rule の source of truth は core 側にある
（CLAUDE.md § Parallel development plan / ADR-0004）。mobile は将来 mock repository
から real（local or API）repository へ無改修で差し替えられる境界を持つべきである。

## Actual Behavior

現状の `mobile/` は **mock-repository prototype** であり、以下の構造的リスクを抱える:

1. **Validation の二重実装による drift**: contract 上 validation の source of truth は
   core（`core/player_repository.py`）だが、mock が backend を模すために
   `mobile/src/validation/playerValidation.ts` で同じ規則（blank / 完全一致重複 /
   大文字小文字・全半角を畳まない）を再実装している。core 側が規則を変えても
   mobile 側は自動追従しない（例: ISSUE-0002 で uniqueness 仕様が拡張された場合）。
2. **Player 型 / error code の手書き複製**: `player.schema.json` /
   `error-shapes.md` から TypeScript 型を**生成していない**（手書き）。schema 変更時に
   mobile の型がずれうる。
3. **Backend integration が未着手**: real repository（local 永続 or API client）が無く、
   `PlayerRepository` interface の API 化（採番責務・楽観ロック・エラー伝播）が未確定。
4. **Offline / sync 不在**: ID 不変キー前提のオフライン編集・衝突解決方針が未設計（S5）。

## Reproduction

1. `core/player_repository.py` の validation 規則を変更する（例: casefold 重複禁止を追加）。
2. `mobile/src/validation/playerValidation.ts` は変更されない。
3. mobile の mock と core で同一入力に対する受理/拒否が食い違う（contract drift）。

## Root Cause

contract（schema / validation-rules / error-shapes）が **単一 source からの型・規則生成**に
なっておらず、mobile が手書きで写経しているため。`docs/contracts/README.md` §
将来拡張余地が「JSON Schema を single source として TypeScript 型を生成する方向」と
述べているが、その codegen はまだ存在しない。

## Fix

未着手（scaffold フェーズのため設計のみ）。方針候補:

- `schemas/*.schema.json` から TypeScript 型を **生成**（json-schema-to-typescript 等）し、
  手書き `models/player.ts` を置き換える。
- validation 規則は contract doc を single source とし、core / mobile が共有できる
  宣言形（規則テーブル）に切り出すか、最低限 contract test を mobile 側にも追加して
  drift を検知する。
- backend 着手時（S5）に `PlayerRepository` の API 実装を追加し、mock を test 専用にする。
- offline / sync は S5 で衝突解決方針（ID 不変キー前提）を ADR 化してから着手する。

## Regression Test

- 現状: `mobile/__tests__/mockPlayerRepository.test.ts` /
  `mobile/__tests__/playerValidation.test.ts` が mock の振る舞いを pin している。
- 将来: schema↔TS 型の整合 / core↔mobile validation の一致を検証する contract test を
  追加する（未実装）。

## Affected Files

- `mobile/src/models/player.ts`
- `mobile/src/validation/playerValidation.ts`
- `mobile/src/repositories/errors.ts`
- `mobile/src/repositories/MockPlayerRepository.ts`
- `docs/contracts/README.md`（codegen 方針）

## Related Worklog

- `docs/worklog/2026-05-25-mobile-scaffold-player-registry.md`

## Related ADRs

- `docs/adr/0004-contract-first-parallel-development-shared-ids-and-separate-frontends.md`
- `docs/adr/0005-contracts-repository-layout-and-freeze-workflow.md`

## Related Commits

- （本 scaffold の commit）

## Notes

ISSUE-0003（並行開発の contract drift）の mobile 具体化。ISSUE-0002（uniqueness 拡張）が
動いた場合、本 issue の drift リスク #1 が顕在化する直接の引き金になる。
