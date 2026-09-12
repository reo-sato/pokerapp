# ADR-0031: L2 の実 IdP 非依存コア（auth_identity + provider 抽象 + mock-first）

## Status

Accepted（**実装済 2026-06-14**。実 IdP（LINE/Google）への HTTP を伴わない L2 コア
= `auth_identity` ストア + OIDC provider 抽象 + claim→principal 解決 + code-exchange エンドポイントを
mock provider で E2E まで実装・テスト。実 provider の HTTP（token 交換 / JWKS）と hosted デプロイは
ADR-0028/0029 のとおり実環境タスクに残す）

## Date

2026-06-14

## Context

ADR-0028（L2 詳細設計）/ ADR-0029（運用）/ ADR-0030（player merge, 実装済）で L2 の前提は揃った。
実 IdP は **インターネット・IdP アプリ登録・client secret・公開 callback** を要し、本サンドボックスでは
実フローを構築・検証できない。一方、L2 の **大半のロジック**（identity バインディング・初回 link・
principal 解決・player merge との接続）は IdP の HTTP に依存せず、**実機/実 IdP なしで実装・テスト可能**。

本 ADR は「実機なしで進められる範囲」を切り出し、**provider を抽象化して mock で先行実装**する方針を定める。

## Decision

### D1. OIDC provider を抽象化（`OidcProvider`）し、検証結果を `VerifiedClaim` に正規化

- `core/oidc.py` に `OidcProvider`（`verify_code(code, *, nonce=None) -> VerifiedClaim`）と
  `VerifiedClaim(provider, subject, display_name_seed?)` を定義。
- callback 後の **アプリ内ロジックは provider 抽象だけに依存**する。実 LINE/Google 実装は token 交換 +
  JWKS 検証を行う薄い層（**実環境タスク**）、テスト/ローカルは `FakeOidcProvider`（code→claim マップ）。
- これで identity 解決・初回 link・principal 発行の全経路を **実 IdP なしで E2E テスト**できる。

### D2. `auth_identity` ストア（`(provider, subject) → player_id`, node-local）

- `core/auth_identity.py`（`AuthIdentity` dataclass）+ `core/auth_identity_repository.py`
  （`AuthIdentityRepository`）。`auth_identity.json`（`.gitignore`, アトミック書き込み）。
- `(provider, subject)` で一意。1 player に複数 provider を link 可（**多対一**, ADR-0028 D1）。
- **node-local（read API / sync 非対象）**: PIN credential（ADR-0027）と同じ扱い。player_id だけが
  会場↔cloud を橋渡しし、sync（ADR-0022）は player_id で収束する（ADR-0029 D1）。
- API: `link` / `get` / `list_for_player` / `unlink`。`link` は同一 (provider,sub) を別 player に張る衝突を
  `AuthIdentityConflictError` で拒否（同一 player への再 link は冪等）。

### D3. claim → player principal 解決（初回 link + canonical, player merge 連結）

`core/oidc.py:resolve_player_for_claim(claim, identity_repo, player_repo) -> player_id`:

- 既存 `auth_identity[(provider, sub)]` があれば、その player_id を **`resolve_canonical`（ADR-0030）**で
  survivor に解決して返す → **merge 後も同一人物の survivor に解決**。
- 無ければ **新規 player を内部採番**（`player_id` は IdP sub から導出しない, ADR-0004/0028 D3）し、
  `display_name_seed` を一意化して registry に作成 → `auth_identity` を張る（初回 link, ADR-0028 D4）。
  会場の既存 player との統合は後でスタッフが **player merge**（ADR-0030）で行う。
- principal token（ADR-0027 の `issue_player_token`）の発行は **API 層**が行う（L1 login と同一形式 =
  認証方式に非依存な合流点, ADR-0027 D3 / ADR-0028 D2）。

### D4. code-exchange エンドポイント（mobile app-driven、テスト可能）

- `POST /api/auth/{provider}/exchange`（body `{code, nonce?}`）→ provider 検証 → claim →
  `resolve_player_for_claim` → principal token を発行して返す。
- ブラウザ redirect の `GET .../login`→`.../callback` 変種より、**mobile（Expo）が IdP SDK / web で得た
  認可コードを backend に POST して交換する app-driven 形**を採る。redirect dance なしで E2E テスト可能で、
  本プロジェクトの mobile front-end に自然。web redirect 変種は実環境で additive 可能。
- **config-gated / 既定 off**: provider 未登録なら本エンドポイントは無効（`unknown_provider` 404）。
  LAN 既定（provider なし）では挙動不変。実 provider の構築（client_id/secret/JWKS）は実環境タスク。

### D5. 実環境に残す範囲（本 ADR の対象外）

- 実 LINE/Google provider の HTTP（token 交換・JWKS 署名検証）実装。
- hosted デプロイ・secret 注入・CORS 絞り・レート制限（ADR-0029 のリスト）。
- web redirect/callback 変種。これらは実 IdP 登録と公開ドメインが要るため実環境で。

## Alternatives Considered

- **実 OIDC を直接実装** → 本サンドボックスで構築・検証不能。→ provider 抽象 + mock-first（D1）。
- **redirect/callback を今作る** → 公開 callback ドメインが要りテスト困難。→ app-driven code-exchange（D4）。
- **auth_identity を sync 対象に** → node-local 秘密を伝播させる。→ credential と同じく sync 非対象（D2）。
- **player_id を sub から導出** → ADR-0004 違反。→ 内部採番 + auth_identity lookup（D3）。

## Consequences

- Positive: L2 の identity ロジックを **実機/実 IdP なしで実装・テスト**でき、player merge（ADR-0030）と
  接続。残りは実 provider の HTTP と hosted 設定のみ。principal は L1 と同一トークンで合流。
- Negative / trade-offs: 実 provider 実装・hosted 設定・web redirect 変種が未了（実環境タスク）。
  auth_identity は node-local なので会場↔cloud の橋渡しは player_id + merge に依存。
- Neutral: `auth_identity.json`（node-local, gitignore）+ `core/auth_identity*.py` + `core/oidc.py` +
  `POST /api/auth/{provider}/exchange`（既定 off）。schema/contract は frozen 化しない（node-local infra,
  credential と同様）。

## Validation / Follow-up

- [x] `core/auth_identity.py` / `core/auth_identity_repository.py`（link/get/list/unlink + 永続 + 衝突）。
- [x] `core/oidc.py`（`OidcProvider` / `VerifiedClaim` / `FakeOidcProvider` / `resolve_player_for_claim`）。
- [x] `POST /api/auth/{provider}/exchange`（provider 抽象注入、既定 off）+ `ViewerApiClient.oidc_exchange`。
- [x] tests: identity repo / 初回 link で player 作成 / 再 login 同一解決 / 多 provider link /
  merge 後 canonical / exchange エンドポイント E2E（fake provider）。
- [ ] 実 LINE/Google provider の HTTP（token 交換 + JWKS）= 実環境タスク。
- [ ] hosted 設定（ADR-0029）/ web redirect 変種 = 実環境タスク。

## Related Files

- `core/auth_identity.py` / `core/auth_identity_repository.py` / `core/oidc.py` / `auth_identity.json`
- `api/server.py`（exchange + provider 注入）/ `api/client.py`（`oidc_exchange`）
- 再利用: `core/auth_token.py`（ADR-0027）/ `core/player_repository.py`（merge, ADR-0030）

## Related Tests

- `tests/test_auth_identity_repository.py` / `tests/test_oidc.py` / `tests/test_viewer_api_oidc.py`

## Related Commits

- 本 ADR の実装 commit（2026-06-14）

## Supersedes / Superseded by

- Supersedes: —（ADR-0028 を実機なしで実装できる範囲に具体化。関連: ADR-0028/0029/0030/0027/0004）
- Superseded by: —
