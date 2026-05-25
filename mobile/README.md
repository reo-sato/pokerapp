# Poker Registry — mobile scaffold (WS3)

Expo + TypeScript scaffold for the **future mobile front-end**. This is **not** the
hand logger and **not** a replacement for the desktop app — it is one consumer of
the shared contract (`docs/contracts/`), alongside core (WS1) and desktop (WS2).

Status: **prototype / UI skeleton on a mock repository.** No real backend, sync,
auth, or persistence. Scope is **Player Registry only** (list / add / rename).

## Why this exists

Per `CLAUDE.md` § Parallel development plan, the mobile front-end can be built
against the frozen contract before core is reachable over an API. The UI depends
only on a `PlayerRepository` interface; today that is an in-memory mock seeded from
the contract fixtures, and it can later be swapped for an API-backed implementation
without touching any screen.

## Run it

```bash
cd mobile
npm install            # add --legacy-peer-deps if peer resolution complains
npm run start          # Expo dev server (press i / a for iOS / Android, w for web)
npm test               # jest (mock repository + validation)
npm run typecheck      # tsc --noEmit
```

A simulator/emulator or the Expo Go app is required to actually render the UI;
the scaffold has not been run on a device in CI.

## Structure

```
mobile/
├── App.tsx                     navigation container + RepositoryProvider
├── index.ts                    Expo root registration
├── src/
│   ├── models/                 Player type + player_id minting (from contract)
│   ├── repositories/           PlayerRepository interface, mock impl, error shapes
│   ├── validation/             display_name rules mirrored from the contract
│   ├── fixtures/               mock seed data copied from docs/contracts/fixtures
│   ├── context/                RepositoryProvider (DI boundary for swapping impls)
│   ├── navigation/             stack param types
│   ├── screens/                PlayerList / AddPlayer / RenamePlayer
│   ├── components/             TextField / PrimaryButton / FeedbackBanner / item
│   └── theme/                  design tokens
└── __tests__/                  repository + validation tests
```

## Contract alignment (do not drift)

The single source of truth is `docs/contracts/` and `core/` — **not** this app.

- **Player shape** follows `docs/contracts/schemas/player.schema.json`
  (`player_id` + `display_name` + `created_at` only).
- **`player_id`** is an opaque, immutable UUID4-hex string (`^[0-9a-f]{32}$`),
  per `docs/contracts/shared-ids.md`. Never parse meaning out of it.
- **Validation** (blank / exact-duplicate, no case/width folding) mirrors
  `docs/contracts/validation-rules.md`. The UI does not validate; it renders the
  repository's `RepositoryError`.
- **Error codes** (`not_found` / `empty_display_name` / `duplicate_display_name` /
  `validation_error`) match `docs/contracts/error-shapes.md` and the core Python
  exception hierarchy 1:1. Branch on `code`, never on `message`.
- **Seed data** is copied verbatim from `docs/contracts/fixtures/player/`.

Drift risk and the eventual real-repository swap are tracked in
`docs/issues/0005-mobile-scaffold-contract-drift-and-backend.md`.

## Out of scope (this scaffold)

Real backend / API, sync, offline, auth/login, session / ledger / point /
settlement screens, delete / merge player, app-store builds, native modules,
analytics, screen render tests.
