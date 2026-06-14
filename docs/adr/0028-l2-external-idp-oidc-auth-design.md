# ADR-0028: L2 外部 IdP（LINE / Google OIDC）認証の詳細設計

## Status

Proposed（**設計のみ**。本 ADR は ADR-0025 の L2 を実装可能なレベルまで具体化する設計記録。
コードは含まない。L2 は LAN-only 前提を変える **hosted モード**を要し、**着手前に運用面（ホスティング /
シークレット管理 / PII / 法令）を固める実装時 ADR が前提**）

## Date

2026-06-14

## Context

ユーザーの将来要件は **「プレイヤーが LINE / Google でアカウントサインアップできる」**。ADR-0025 で
これを L2（外部 IdP / OAuth2・OIDC）として方針確定し、`(provider, subject) → player_id` の
**`auth_identity` バインディング**を素描した。本 ADR はその **詳細設計**を行う。

L2 が触れる前提（ADR-0025 §4）:

- **`player_id` は内部採番・不変**（ADR-0004）。外部 subject から player_id を**導出しない**
  （IdP 失効・乗り換え・複数 IdP link で player_id が動かないため）。`auth_identity` は単なる lookup。
- 現行 deployment は **LAN 限定・無認証・オフライン可・外部システムなし**。外部 IdP は
  **インターネット接続・IdP アプリ登録・client secret・到達可能 callback・サーバ側トークン検証**を要し、
  この前提から逸脱する → **hosted / cloud モード**として LAN 会場モード（name-pick）と**共存**させる。
- L1（ADR-0027）で導入する **player principal 解決レイヤ + 署名トークン**を再利用する（合流点）。

## Decision

### D1. `auth_identity` バインディング（多対一の lookup テーブル）

`(provider, subject) → player_id` を保持する新ストア。player_id は**ここでは採番しない**（既存 player に
紐づけるか、内部採番で新規 player を作る）。

- ドメイン: `core/auth_identity.py`（`AuthIdentity` dataclass）。フィールド:
  `provider`（`"line" | "google"`）/ `subject`（IdP の `sub`）/ `player_id` / `linked_at` /
  `display_name_seed?`（初回 link 時の表示名候補のみ。PII 最小化）。
- リポジトリ: `core/auth_identity_repository.py`。`(provider, subject)` で一意。1 player が複数 provider を
  link 可（**多対一**: LINE と Google の両方を同じ player_id に）。
- 永続: hosted モードでは DB（または `auth_identity.json` 相当）。**LAN 会場モードでは無効**（L2 は
  hosted 専用）。L1 の credential と同様 **sync snapshot には含めない**（node-local の認証メタデータ。
  player_id だけが両モードを橋渡しし、sync = ADR-0022 で収束）。

### D2. OIDC Authorization Code フロー（サーバ側でトークン検証）

```
1. GET  /api/auth/{provider}/login
     → state, nonce, PKCE(code_verifier) を生成・保存し、IdP の authorize URL へ 302。
2. （player が IdP で同意）
3. GET  /api/auth/{provider}/callback?code=...&state=...
     → state 照合 → token endpoint で code を id_token に交換
     → id_token(JWT) を JWKS で署名検証（iss / aud=client_id / exp / nonce）
     → claim から sub を取り出す
     → auth_identity[(provider, sub)] を引く
         ├ ヒット   : その player_id を解決
         └ ミス     : 初回 link フロー（D4）へ
     → player principal 署名トークン（ADR-0027 と同形式）を発行し、
       Set-Cookie（HttpOnly/Secure/SameSite）または app への deep-link で返す。
```

- **id_token はサーバ側で検証して破棄**する。クライアントに IdP トークンを渡さない。
- 発行する **player bearer は L1 と同一**（`v1.<player_id>.<exp>.<hmac>`）。以降の API 認可は principal
  レイヤが処理し、**エンドポイント契約は L0/L1/L2 で不変**。
- provider 設定: config `viewer_api.oidc.{google,line}: {client_id, client_secret, redirect_uri, scopes}`。
  Google は OIDC discovery（`.well-known/openid-configuration`）、LINE Login は OIDC 互換（id_token は JWT、
  LINE の JWKS / issuer で検証）。実エンドポイント値は実装時に確定。

### D3. player_id は外部 subject から導出しない（不変条件の維持）

- player_id は引き続き **内部 UUID 採番**。`auth_identity` は `(provider, sub)` → player_id の写像にすぎない。
- これにより: IdP アカウント失効・機種変更・provider 乗り換えでも player_id は不変。session / seating /
  ledger / settlement / order は player_id キーのまま影響を受けない。

### D4. 初回 link モデル（本 ADR の主要プロダクト判断）

未知の `(provider, sub)` で初回ログインしたとき:

- **既定 = 新規 player を内部採番**し、`display_name`（IdP profile の名前を seed に、重複時はサフィックス）を
  付けて `auth_identity` を張る。以後の同 sub ログインは常に同じ player_id に解決される。
- **重複 player の回避**は **staff による link / merge** で行う（会場で既に name-pick 用 player が存在する
  ケース）。ただし **player merge は現状 scope 外**（CLAUDE.md）であり、本設計の**依存・前提課題**として
  明記する（L2 実装前に「同一人物の 2 player_id を merge」フローが必要）。
- 代替の self-claim（既存 player の PIN を併せて提示して link）は L1 と組み合わせ可能だが、初期は
  「新規採番 + staff link/merge」を既定とする（運用が単純で、player_id 不変条件と整合）。

### D5. hosted モードと LAN 会場モードの共存

- L2 は **hosted（cloud）モード**でのみ有効化する。LAN 会場モードは name-pick（L0）/ PIN（L1）のまま。
- 両モードは **player_id を共有キー**として橋渡しし、データは **sync（ADR-0022）で player_id を跨いで収束**。
- `auth_identity` は hosted ノード固有（sync 対象外）。「会場で作った player」と「hosted でサインアップした
  player」を同一視するのは D4 の link/merge の責務。

### D6. PII 最小化 / トークン保管

- 保存するのは **`sub` + `player_id` + 任意の `display_name_seed`** のみ。**email / 電話番号 / 完全な
  profile / IdP の access_token・refresh_token は保存しない**（本システムは認証のみで IdP API を叩かない）。
- id_token は検証後**破棄**。自前の player bearer は **stateless HMAC**（サーバ側セッションストアなし、L1 と
  同じ）。署名 secret と client_secret は**シークレットマネージャ**（環境変数 / cloud secret store）で管理し、
  リポジトリにコミットしない。
- データ保持・削除（player が link 解除/退会したら `auth_identity` 行を削除）方針は実装時 ADR で確定。

### D7. 運用面は実装時 ADR が前提（本 ADR は設計まで）

L2 着手時に **別 ADR**（hosting/ops）で確定する事項 → **ADR-0029 で確定済み**（運用設計）:

- ホスティング形態（**マネージド PaaS**）、シークレット管理（**PaaS env / secret store**）、
  IdP アプリ登録（**LINE + Google**）、レート制限 / ログ PII マスキング、APPI のデータ保持・削除要件。
- トポロジー = **会場 source-of-truth + cloud は player ミラー**（cloud は会計を originate せず、
  player signup / 閲覧 / 注文 + sync のみ公開、ADR-0029 D1/D6）。
- 本 ADR は **アプリ内設計**（auth_identity モデル / OIDC フロー / principal 合流 / PII 最小化方針）を確定し、
  運用詳細は **ADR-0029** が担う。

## Alternatives Considered

- **外部 sub を player_id に採用（IdP を採番元に）** → ADR-0004 違反。失効・乗り換え・複数 link で破綻。
  → 内部採番 + auth_identity lookup（D3）。
- **OAuth を LAN モードに直接載せる** → オフライン・無外部システム前提を破壊。→ hosted モードに分離（D5）。
- **初回ログインで常に既存 player へ自動マッチ（氏名等で）** → 誤同定リスク。→ 既定は新規採番 + staff
  link/merge（D4）。
- **IdP の access/refresh トークンを保存して profile を同期** → PII / 漏洩面が増え、認証目的に不要。
  → sub のみ保存（D6）。
- **stateful セッション（サーバ側ストア）** → スケール・sync で状態管理が増える。→ L1 と同じ stateless HMAC。

## Consequences

- Positive: 「LINE / Google でサインアップ」を **player_id 不変・エンドポイント契約不変**のまま実現する
  設計が固まる。L1 の principal レイヤ + トークンを再利用し、L0/L1/L2 が同じ認可面に load。LAN と hosted が
  player_id + sync で共存。PII を最小化。
- Negative / trade-offs: hosted デプロイ・シークレット管理・IdP 登録・TLS・法令対応という**運用コスト**が
  発生（LAN 運用より重い）。**player merge（scope 外）が前提依存**として必要になる。実装時 ADR（ops）が
  着手前提。
- Neutral: `core/auth_identity*.py` + auth callback エンドポイント群 + config `viewer_api.oidc.*` が増える
  （いずれも hosted 有効時のみ。LAN 既定では未使用）。

## Validation / Follow-up

- [ ] **前提**: 運用面（hosting / secret / PII / 法令）の実装時 ADR（D7）。
- [ ] **前提/依存**: player merge フロー（同一人物の 2 player_id 統合, 現状 scope 外）。
- [ ] `core/auth_identity*.py` + repository + OIDC callback（state/nonce/PKCE/JWKS 検証）。
- [ ] principal トークン発行を L1 と共通化（ADR-0027 D3 のユーティリティ再利用）。
- [ ] 初回 link UX（新規採番 → staff link/merge）と link 解除（退会）フロー。
- [ ] provider 別の id_token 検証（Google OIDC discovery / LINE Login）の結合テスト（モック IdP）。

## Related Files

- 新規（実装時）: `core/auth_identity.py` / `core/auth_identity_repository.py`,
  `api/` の OIDC callback ルート, config `viewer_api.oidc.*`
- 再利用: ADR-0027 の principal 解決 + 署名トークン
- 不変: `core/player.py` / player schema（`1.0` frozen）/ `core/sync.py`（snapshot は player_id ベース）

## Related Tests

- 実装時に追加（モック IdP での OIDC フロー / auth_identity lookup / 初回 link）。

## Related Commits

- 本 ADR と同じ commit（設計記録のみ、コードなし）

## Supersedes / Superseded by

- Supersedes: —（ADR-0025 L2 を詳細化。関連: ADR-0025 / ADR-0027（principal レイヤ共通）/ ADR-0004
  （player_id 不変）/ ADR-0022（sync は player_id で収束）/ ISSUE-0019）
- Superseded by: —（運用面は **ADR-0029**（hosted 運用設計）で確定 = 本 ADR の前提）
