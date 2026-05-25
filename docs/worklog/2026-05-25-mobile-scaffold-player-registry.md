# Worklog: Mobile scaffold — Player Registry (WS3)

## Date

2026-05-25

## Scope / Task

WS3 mobile front-end の **scaffold / UI skeleton / mock repository** を新設する
（Phase 1 の mobile タスク）。Player Registry のみ（list / add / rename）。本実装・
real backend・sync・auth・session/ledger/settlement は scope 外。

## Goal

- Expo + TypeScript で iOS / Android 両対応前提の app scaffold を `mobile/` に追加する。
- UI は `PlayerRepository` interface のみに依存し、in-memory mock を注入で差し替える。
- mock は contract fixtures を seed にし、contract（schema / validation-rules /
  error-shapes / shared-ids）と矛盾しない。
- hand logger / core 既存コードに一切手を入れない。

## Changed Files

新規（`mobile/`）:

- `mobile/package.json` — Expo SDK 52 + RN 0.76 + React Navigation 7 + TS、scripts。
- `mobile/app.json`, `mobile/babel.config.js`, `mobile/tsconfig.json`,
  `mobile/jest.config.js`, `mobile/index.ts`, `mobile/.gitignore` — Expo project config。
- `mobile/App.tsx` — NavigationContainer + native-stack + RepositoryProvider。
- `mobile/src/models/player.ts` — `Player` 型と `player_id` 形式（contract 由来）。
- `mobile/src/models/ids.ts` — mock 用 `player_id`（UUID4 hex 32）採番。
- `mobile/src/repositories/PlayerRepository.ts` — interface 契約。
- `mobile/src/repositories/errors.ts` — `RepositoryError` + `code`（error-shapes 準拠）。
- `mobile/src/repositories/MockPlayerRepository.ts` — in-memory 実装。
- `mobile/src/validation/playerValidation.ts` — display_name 規則（contract mirror）。
- `mobile/src/fixtures/players.ts` — contract fixtures から copy した seed。
- `mobile/src/context/RepositoryContext.tsx` — DI 境界。
- `mobile/src/navigation/types.ts` — stack param 型。
- `mobile/src/screens/{PlayerList,AddPlayer,RenamePlayer}Screen.tsx` — 画面。
- `mobile/src/components/{PlayerListItem,PrimaryButton,TextField,FeedbackBanner}.tsx` — 部品。
- `mobile/src/theme/theme.ts` — design tokens。
- `mobile/__tests__/{mockPlayerRepository,playerValidation}.test.ts` — テスト。
- `mobile/README.md` — mobile 専用 README。

docs:

- `CLAUDE.md` — ディレクトリ構成に `mobile/` 追加、実装状況表に mobile scaffold 行追加、
  Phase 1 done criteria を mobile 着手済に更新。
- `CHANGELOG.md` — Unreleased / Added に mobile scaffold。
- `docs/issues/0005-mobile-scaffold-contract-drift-and-backend.md` — 新規 open issue。
- `docs/decision-log.md` — ISSUE-0005 を index 追加。

## Expected Behavior

- player 一覧（空状態あり、fixtures 初期表示）、name タップで rename、+ で add。
- add / rename で blank・duplicate を field error として表示、成功で list へ戻り更新。
- validation / error は core と同じ `code` 体系で repository から返り、UI は表示のみ。
- mock は fixtures を seed にし、`player_id` は contract 形式（`^[0-9a-f]{32}$`）。

## Implemented Behavior

期待どおり実装。要点:

- UI は `usePlayerRepository()` 経由で interface にのみ依存。`RepositoryProvider` の
  差し替えで mock → API へ無改修移行できる境界。
- validation は mock（repository 層）が `validation-rules.md` を mirror し、screens は
  `RepositoryError.code` で field error / banner を出し分けるのみ（business logic 非複製）。
- mock の duplicate は strip 後完全一致のみ、case/width は畳まない（S1 scope, ISSUE-0002）。
- rename の自己一致は no-op 許容、`player_id` 不変。

## Test Results

- `mobile/ $ npx jest` → **2 suites / 17 tests passed**
  （mock repository: seed / 作成順 / id 形式 / strip / blank reject / duplicate reject /
  case 区別 / rename / no-op rename / 他名重複 reject / not_found / 空 seed。
  validation: strip / empty / duplicate / self-match / no fold）。
- `mobile/ $ npx tsc --noEmit` → **TYPECHECK OK**（strict + noUncheckedIndexedAccess）。
- `mobile/ $ npx expo config --type public` → 解決成功（sdkVersion 52.0.0, platforms ios/android）。
- 既存 Python テスト（`tests/`）には未介入（hand logger / core ファイル無改修）。

## Mismatches Found During Testing

- `noUncheckedIndexedAccess` により `ids.ts` の `bytes[6]/[8]` と test の配列
  destructuring が `possibly undefined` で typecheck fail。→ Fixes 参照。
- それ以外、期待挙動との乖離なし。

## Fixes Applied

- `ids.ts`: `bytes[6]` → `(bytes[6] ?? 0)` 等で undefined 安全化。
- test: `const [alice] = ...` → `const alice = (await repo.listPlayers())[0]!` に変更。

## Remaining Gaps / Out-of-Scope

- [ ] real backend / API repository（mock のみ）。ISSUE-0005。
- [ ] schema → TypeScript 型 codegen（現状手書き、drift 余地）。ISSUE-0005。
- [ ] screen render / smoke test（@testing-library/react-native 未導入）。
- [ ] device / emulator 上での実機描画確認（CI 環境では未実施）。
- [ ] session / ledger / point / settlement 画面、delete / merge、offline / sync、auth。

## Related ADRs

- `docs/adr/0004-contract-first-parallel-development-shared-ids-and-separate-frontends.md`
- `docs/adr/0005-contracts-repository-layout-and-freeze-workflow.md`

## Related Issues

- `docs/issues/0005-mobile-scaffold-contract-drift-and-backend.md`
- `docs/issues/0003-parallel-dev-contract-drift.md`（mobile 具体化）
- `docs/issues/0002-display-name-uniqueness-scope.md`（drift トリガ候補）

## Related Commits

- （本 scaffold の commit）
