# Worklog: L2 の実 IdP 非依存コア（ADR-0031）

## Date

2026-06-14

## Scope / Task

「実機なしで進められる」範囲として、L2（外部 IdP）のうち実 LINE/Google の HTTP を伴わないコアを実装する。
provider を抽象化して mock で E2E までテストし、実 provider の HTTP と hosted 設定は実環境タスクに残す。

## Goal

auth_identity バインディング + OIDC provider 抽象 + claim→principal 解決 + code-exchange エンドポイントを
実装し、初回 link・再 login 同一解決・player merge 連結・principal 合流（L1 と同形トークン）をテストで固める。
LAN 既定（provider 未構成）は挙動不変。

## Changed Files

- `core/auth_identity.py`（新規）: `AuthIdentity`（(provider,sub)→player_id, display_name_seed のみ保持）。
- `core/auth_identity_repository.py`（新規）: `AuthIdentityRepository`（link/get/list_for_player/unlink +
  `auth_identity.json` 永続 + `AuthIdentityConflictError`）。node-local（read API / sync 非対象）。
- `core/oidc.py`（新規）: `VerifiedClaim` / `OidcProvider`(Protocol) / `FakeOidcProvider` /
  `resolve_player_for_claim`（既存は resolve_canonical、初回は新規 player 作成 + link、display_name 一意化）。
- `api/server.py`: `create_app(..., oidc_providers, identity_repo)` + `POST /api/auth/{provider}/exchange`
  （provider 検証 → 解決 → ADR-0027 principal トークン）。既定 provider なし = `unknown_provider` 404。
- `api/client.py`: `oidc_exchange(provider, code)`（token を保持）。
- `.gitignore`: `auth_identity.json`。
- tests（新規）: `tests/test_auth_identity_repository.py` / `tests/test_oidc.py` /
  `tests/test_viewer_api_oidc.py`。
- docs: `docs/adr/0031-...md` / decision-log / error-shapes（`unknown_provider`/`invalid_idp_code`/
  `identity_conflict`）/ CLAUDE.md / CHANGELOG。

## Key Decisions（ADR-0031）

- **provider 抽象 + mock-first**: callback 後のロジックは provider 抽象だけに依存。`FakeOidcProvider` で
  全経路を実 IdP なしで E2E テスト。
- **app-driven code-exchange**（`POST .../exchange`）: redirect/callback dance を避け、mobile が得た認可
  コードを backend に POST。テスト可能で Expo に自然。
- **node-local auth_identity**（sync 非対象, credential と同じ）。player_id は IdP sub から導出しない
  （ADR-0004）。会場との統合は player merge（ADR-0030）。principal は ADR-0027 トークンで L1 と合流。

## Expected / Implemented Behavior

- 初回 exchange: 新規 player 作成（display_name = seed 由来・衝突は一意化）+ auth_identity link + token 発行。
- 再 exchange: 同一 player に解決（新規作成しない）。merge 後は survivor に解決。
- provider 未構成: 404 unknown_provider（LAN 既定で挙動不変）。検証失敗: 401 invalid_idp_code。

## Test Results

- `pytest tests/test_auth_identity_repository.py tests/test_oidc.py tests/test_viewer_api_oidc.py -q` — 17 passed。
- `pytest tests/ --ignore=tests/test_vision.py -q` — **597 passed, 0 skipped**（既存 580 + 17）。
- `ruff check .` — clean。

## Mismatches Found During Testing

- exchange token の principal テストで、注文 POST が `orders_writable=False` のため 503 が先に返った →
  テストの app を `orders_writable=True` で構築して principal（forbidden）経路を検証。

## Remaining Gaps / Out-of-Scope（実環境タスク）

- 実 LINE/Google provider の HTTP（token 交換 + JWKS 署名検証）。
- hosted デプロイ（ADR-0029: env override / cloud モード config / CORS 絞り / レート制限）。
- web redirect/callback 変種。

## Related ADRs / Issues

- ADR-0031（本件）/ ADR-0028（L2 設計）/ ADR-0029（運用）/ ADR-0030（merge）/ ADR-0027（principal）/
  ADR-0004（player_id 不変）

## Related Commits

- 本 commit
