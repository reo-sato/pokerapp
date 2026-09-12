# ADR-0029: L2 hosted モードの運用設計（ホスティング / sync トポロジー / シークレット / PII）

## Status

Proposed（**運用設計のみ**。本 ADR は ADR-0028（L2 外部 IdP）の前提となる運用面の判断を確定する
記録で、コードは含まない。L2 本体の実装は ADR-0028 + 本 ADR の決定に従う別タスク）

## Date

2026-06-14

## Context

ADR-0028（L2 = LINE / Google OIDC）は、外部 IdP が **インターネット接続・公開 callback・
client secret・サーバ側トークン検証**を要し、現行の **LAN 限定・無外部システム・オフライン可**前提から
逸脱するため、**hosted（cloud）モード**として LAN 会場モードと共存させる設計とした。ADR-0028 §D7 は
運用面（ホスティング / シークレット管理 / IdP 登録 / PII・法令 / レート制限 / ログ）を「実装時 ADR」に
委譲しており、本 ADR がそれを担う。

対象は **小規模クラブ・個人配信向け**（CLAUDE.md）であり、運用負荷とコストを最小化したい。
ユーザー決定（2026-06-14）:

- **トポロジー = 会場 source-of-truth + cloud は player ミラー**
- **ホスティング = マネージド PaaS**
- **IdP = LINE と Google の両方**

## Decision

### D1. トポロジー: 会場が source-of-truth、cloud は player 向け read-mostly ミラー

- **会計（ledger / settlement）・ハンドログ・session は会場 PC が source of truth**。cloud は
  これらを **read ミラー**として保持し（player が自分の履歴・会計を閲覧）、**originate しない**。
- **cloud で発生する write は限定**する:
  - player の **サインアップ（L2 OIDC）** → `auth_identity` バインディング（**node-local・sync 非対象**,
    ADR-0028 D1）。
  - player の **注文リクエスト（pending）** → `order_requests`。**確定（ledger 化）は会場スタッフ**が行う
    （staff-in-the-loop, ADR-0018）。cloud は ledger を書かない。
- **player registry（players.json）は双方向 sync**（sync snapshot の 4 パスに含まれる）。cloud で
  サインアップした player（新規 player_id, ADR-0028 D4）は sync で会場に現れ、会場スタッフが既存の
  name-pick player と **merge**（依存・前提課題）。`auth_identity` / PIN credential は cloud-local で sync しない。
- **sync 方向**（ADR-0022 の on-demand pull/merge を会場主導で実行。会場は LAN/NAT 内で公開不可のため
  会場が起点）:
  - 会場 → cloud: 会場が会計/ハンド/registry の確定スナップショットを cloud にミラー（push merge）。
  - cloud → 会場: 会場が cloud の player signup / 注文リクエストを取り込み（pull）。
- **帰結**: cloud 侵害時も **会計の真実は会場側で保全**される（cloud は player の自己申告データ +
  会場由来ミラーのみ）。ネット断時は会場が **LAN 単独で継続**（オフライン可を維持）、復帰後に sync で収束。

### D2. ホスティング: マネージド PaaS（Render / Railway / Fly.io 等）

- TLS 終端・常時 HTTPS・自動デプロイ・ヘルスチェック・シークレットストアが内蔵で、**運用負荷が最小**。
  小規模クラブ / 個人配信に適し、無料〜低額枠で始められる。
- **単一の小コンテナ**（FastAPI + uvicorn, `[api]` extra）で 1 インスタンス。公開ドメイン
  （PaaS 既定 `*.onrender.com` 等、または独自ドメイン）で OIDC callback を受ける。
- **永続の注意点**: 現行永続は JSON ファイル + アトミックリネーム。PaaS は filesystem が **ephemeral**
  なことが多く、再起動でファイルが消える。→ cloud node は **persistent volume を付与**するか、将来
  **repository を DB backed に差し替える**（repository interface は ADR-0020 で frozen のため backend
  差し替えで UI / 契約は不変）。初期は persistent volume + JSON で足り、段階移行とする（follow-up）。

### D3. IdP: LINE と Google 両方を登録

- **LINE Login channel** + **Google OAuth 2.0 client** を各々登録。`client_id` / `client_secret` /
  `redirect_uri` を取得。`redirect_uri = https://<hosted-domain>/api/auth/{provider}/callback`。
- `auth_identity` は **多対一**（1 player が LINE と Google を両 link 可, ADR-0028 D1）。Google は
  OIDC discovery（`.well-known/openid-configuration`）、LINE は OIDC 互換（id_token JWT を LINE の
  JWKS / issuer で検証）。

### D4. シークレット管理

- `client_secret`（×2）/ `player_token_secret`（ADR-0027）/ `staff_token`（sync 用, ADR-0021）は
  **PaaS の環境変数 / secret store** で注入し、**リポジトリにコミットしない**。`config_default.json` には
  値を置かず、**env override** で上書きできるようにする（config ローダへの env 対応は L2 実装の follow-up）。
- **`player_token_secret` は cloud では固定値**（env）にする。ephemeral だと再起動で全 player の
  トークンが失効するため（ADR-0027 D3 の ephemeral 既定は LAN 用）。

### D5. PII 最小化 / 法令（個人情報保護法 = APPI 前提）

- 日本の小規模事業者前提 → 主に **APPI**（GDPR は EU 居住者を対象にする場合のみで、当面 scope 外）。
- **保存は `sub` + `player_id` + `display_name_seed` のみ**（ADR-0028 D6）。email / 電話 / 完全 profile /
  IdP の access・refresh トークンは**保存しない**。id_token は検証後破棄。
- **利用目的の明示**（本人のハンド履歴・会計閲覧、サインアップ）/ 第三者提供なし。サインアップ画面に
  **プライバシーポリシーを掲示**（運用要件）。
- **開示・訂正・削除請求**への対応: 退会で `auth_identity` 行と cloud-local データを削除。player_id 自体は
  会場の会計整合のため保持しうるが、連絡先非保持 = 仮名化されており単体では個人を特定しない。
- **データ保持**: 会計記録は店舗運営上必要な期間（会場側が真実）。cloud ミラーは閲覧目的で同等。

### D6. 攻撃面の最小化（cloud 公開エンドポイントの限定）

- cloud が公開するのは **player read + signup（auth）+ 注文 POST + sync（staff-token gate）** のみ。
  **直接の会計 write エンドポイント**（`/api/staff/.../ledger-entries` / `settlement/commit` /
  `payment` / 注文 confirm 等）は **cloud では無効化**する（会計は会場が真実 = D1）。sync の
  `snapshot/merge` はミラー受信に必要なので残す。→ L2 実装で「cloud 向けに会計 write 無効・sync と
  order は有効」のモードを config で切替（follow-up）。
- **レート制限**: `auth/login`・注文 POST に throttle（PaaS の WAF/プロキシ or アプリ内）。
- **ログ PII マスキング**: PIN / id_token / client_secret / Authorization ヘッダをログに出さない。
- **CORS**: 現状 `*`（LAN 前提, ADR-0017）。hosted では **player web origin に絞る**（follow-up）。
- HTTPS 強制（PaaS 既定）。

## Alternatives Considered

- **cloud authoritative / 一本化**（会場も cloud API を叩く）→ オフライン可前提を放棄。小規模会場の
  ネット断耐性を失う。→ 不採用（D1 = 会場 source-of-truth）。
- **VPS 自前 / 本格 cloud(AWS/GCP)** → TLS 更新・監視・IAM 等の運用が小規模に重い / 設定過剰。
  → 不採用（D2 = マネージド PaaS）。
- **マネージド DB へ即時移行** → 初期は persistent volume + JSON で足りる。interface frozen なので
  後で差し替え可。→ 段階移行（D2 follow-up）。
- **email 等 PII を保存して profile 同期** → 認証に不要で漏洩面が増える。→ sub のみ（D5）。
- **cloud にも会計 write を開放** → 攻撃面が会計に及ぶ。→ cloud は会計 write 無効（D6）。

## Consequences

- Positive: 会計の真実を **会場側で保全**したまま、player の LINE/Google サインアップと閲覧・注文を
  cloud で提供できる。運用負荷は PaaS 内蔵機能で最小。PII 最小。cloud の攻撃面が player read + signup +
  order + sync に限定される。オフライン可を維持。
- Negative / trade-offs: cloud node の **永続（ephemeral FS）**・**ドメイン/secret 運用**・**CORS 絞り込み**・
  **env override**・**レート制限/ログマスキング**・**会計 write 無効モード**の実装が新たに要る。
  **player merge（scope 外）**が依然前提。
- Neutral: L2 本体（OIDC コード）+ cloud デプロイ設定は ADR-0028 + 本 ADR に従う別タスク。

## Validation / Follow-up（L2 実装の着手リスト）

- [ ] cloud node の永続方針確定（persistent volume vs DB backed repository, ADR-0020 interface で差し替え）。
- [ ] config の **env override**（secret 注入）+ **CORS を hosted origin に絞る**。
- [ ] cloud 向け **会計 write 無効・sync/order 有効モード**（config 切替, D6）。
- [ ] `auth/login`・注文 POST の **レート制限** + ログ **PII マスキング**。
- [ ] **LINE Login channel / Google OAuth client 登録**（redirect_uri 確定）。
- [ ] **プライバシーポリシー掲示** + 退会（削除）フロー（APPI）。
- [ ] **依存**: player merge（会場 name-pick player ↔ cloud signup player の統合, scope 外）。
- [ ] L2 本体実装（ADR-0028: `auth_identity` + OIDC callback + principal トークン共通化）。

## Related Files

- 設計参照: ADR-0028（L2 本体）/ ADR-0027（principal トークン）/ ADR-0022（sync）/ ADR-0020
  （repository interface frozen = backend 差し替え可）/ ADR-0021（staff token）/ ADR-0017（CORS / LAN 前提）
- 実装時（L2）: `core/auth_identity*.py`, `api/`（OIDC callback / cloud モード config）, deploy 設定

## Related Tests

- L2 実装時に追加（モック IdP / cloud モードのエンドポイント可視性 / sync ミラー方向）。

## Related Commits

- 本 ADR と同じ commit（運用設計記録のみ、コードなし）

## Supersedes / Superseded by

- Supersedes: —（ADR-0028 §D7 が委譲した運用判断を確定。関連: ADR-0028 / ADR-0025 / ADR-0022 / ADR-0020）
- Superseded by: —
