# Worklog: mobile に本人認証 UI を反映（L1 PIN / L2 サインアップ）

## Date

2026-06-14

## Scope / Task

L1 PIN（ADR-0027）/ L2 外部 IdP（ADR-0031）を mobile（Expo/RN）UI に反映する。既定の name-pick を壊さず、
本人確認が要る会場向けに login / signup を additive に載せる。実機不要（mock + typecheck + web export）。

## Goal

mobile が PIN ログイン・LINE/Google サインアップで principal トークンを取得し、self-write（注文 POST）に
Bearer で付与できるようにする。UI 導線も追加。

## Changed Files

- `mobile/src/api/types.ts`: `AuthSession` 追加。`Player` を schema 1.2 に追従（`updated_at` /
  `merged_into` / `merged_at` optional）。
- `mobile/src/api/repository.ts`: `ViewerRepository` に `login` / `oidcExchange` / `currentPrincipal` /
  `clearAuth` を additive。
- `mobile/src/api/httpRepository.ts`: principal トークン保持 + `post(..., withAuth)` で注文 POST に
  `Authorization: Bearer`。`POST /api/auth/login`・`/api/auth/{provider}/exchange`。
- `mobile/src/api/mockRepository.ts`: 同 interface を mock 実装（デモ PIN=1234 / OIDC code=demo-good、
  サインアップ player を in-memory 保持し getPlayer/listPlayers から参照可、再交換は同一 player に解決）。
- `mobile/src/screens/AuthScreen.tsx`（新規）: pin モード（PIN 入力 → login）/ oidc モード（LINE/Google
  ボタン → oidcExchange → getPlayer）。error code を日本語メッセージにマップ。
- `mobile/src/screens/PlayerSelectScreen.tsx`: 各 player に「PIN でログイン」、footer に「LINE / Google で
  サインアップ」導線。name-pick タップは不変。
- `mobile/App.tsx`: `auth` route 追加（mode + player）+ AuthScreen 結線。
- `mobile/src/api/mockRepository.test.ts`: login（正/誤/短 PIN/unknown）+ oidcExchange（作成・再解決・不正）
  のテスト。
- docs: CLAUDE.md（mobile status 行 + tree）/ CHANGELOG。

## Key Decisions

- **name-pick 既定は不変**: login / signup は additive 導線。`player_auth=off` の会場は従来どおり選ぶだけ。
- principal トークンは **repository インスタンスが保持**し、self-write のみに付与（read は無認証のまま）。
- L2 の **認可コード取得（IdP SDK / web redirect）は実環境タスク**。UI はその差し込み点で、mock/未構成
  サーバではデモコードを送る（ADR-0031 D5）。
- mock の OIDC サインアップ player を getPlayer/listPlayers に載せ、後続画面（sessions 等）へ遷移可能に。

## Expected / Implemented Behavior

- PIN ログイン成功 → トークン保持 → 注文 POST に Bearer 付与。誤 PIN=invalid_pin、短 PIN=pin_too_short。
- LINE/Google サインアップ → player 作成（再交換は同一）→ sessions へ遷移。
- name-pick は従来どおり（タップで sessions）。

## Test Results

- `npm run typecheck`（tsc --noEmit）— clean。
- `npm test`（tsx --test）— **11 passed**（既存 8 + auth 3）。
- `npm run export:web`（expo export --platform web）— 成功（index bundle 出力、AuthScreen 込みで build）。
- Python 側変更なし（前 commit 618 passed のまま）。

## Mismatches Found During Testing

- mock の OIDC player に `updated_at` を持たせたため `Player` 型に additive フィールドが必要 → types.ts を
  schema 1.2 に追従。

## Remaining Gaps / Out-of-Scope

- 実 IdP の認可コード取得（Expo の LINE/Google SDK or web redirect）= 実環境タスク（ADR-0031 D5）。
- 注文 401 時の自動ログイン誘導（現状は ErrorView 表示 → 戻ってログイン）。PIN 自己設定 UI（self-enroll）。

## Related ADRs / Issues

- ADR-0027（L1 PIN）/ ADR-0031（L2 IdP コア）/ ADR-0030（merge canonical principal）/ ADR-0017（mobile）

## Related Commits

- 本 commit
