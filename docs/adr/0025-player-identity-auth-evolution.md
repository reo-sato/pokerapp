# ADR-0025: player アイデンティティ / 認証の進化方針（name-pick → PIN → 外部 IdP）

## Status

Accepted（方針として確定。実装は段階的・後続。本 ADR は「将来の外部サインアップを spec が
許容する」ための土台を固めるもので、L1/L2 の実装着手は別タスク）

## Date

2026-06-13

## Context

現状のプレイヤー識別は **name-pick（一覧から自分を選ぶ・無認証・LAN 限定）**（ISSUE-0019 v1）。
near-term の候補として **per-player PIN** が挙がっていたが、ユーザーの将来要件は
**プレイヤーが LINE / Google でアカウントサインアップ（外部 IdP / OAuth・OIDC）できること**。

この将来要件は 2 つの前提に触れる:

- **`player_id` の不変条件**（ADR-0004 / `shared-ids.md`）: player_id は **アプリ内採番・不変・
  外部システム非依存**。session/seating/ledger/settlement/order は全て player_id をキーにする。
- **現行の deployment 前提**: viewer API は **LAN 限定・無認証・オフライン可・外部システムなし**。

PIN を場当たり的に入れると、後で外部 IdP に移行するとき throwaway / 二重実装になりかねない。
ここで識別と認証のレイヤを分離し、name-pick → PIN → 外部 IdP が **additive な進化**になる土台を固める。

## Decision

1. **`player_id` を唯一の安定な内部アイデンティティキーとして不変に保つ**。認証方式が増えても
   player_id は変わらない（ADR-0004 の「採番はアプリ内で完結」を維持。外部 subject から player_id を
   導出しない＝IdP 変更・失効で player_id が動かない）。全データは player_id でキーし続ける。
2. **認証はレイヤとして player に additive に紐づく**（player_id を置き換えない）:
   - **L0（実装済）**: name-pick / 無認証 / LAN 限定（ISSUE-0019 v1）。
   - **L1（将来・任意）**: **per-player PIN** = player に紐づく credential（`players.json` に
     additive な `pin_hash` 等。平文で持たない）。LAN 内でプレイヤー自身の write（注文キャンセル等）を
     本人認証する。player schema への additive 追加で済む。
   - **L2（将来）**: **外部 IdP（LINE / Google, OAuth2/OIDC）**。`(provider, subject) → player_id` の
     **`auth_identity` バインディング**（多対一: 1 player が LINE と Google 両方を link 可）。
     初回ログイン時に既存 player へ link（スタッフ確認 or 自動）するか新規 player_id を発行する。
     IdP トークンはサーバ側で検証し、API は「player principal = player_id」を導出する。
3. **「player principal 解決レイヤ」を API 境界の概念として導入**する（実装は段階的）。受け取った
   credential（無し / PIN / IdP トークン）を player_id に解決し、「player は自分の player_id に対してのみ
   操作可」を認可する。これにより L1/L2 は principal 解決の差し替えで載る（エンドポイント契約は不変）。
4. **deployment 含意（重要）**: 外部 IdP は **インターネット接続・IdP アプリ登録・client secret・
   到達可能な callback・サーバ側トークン検証**を要し、現行の「LAN 限定・無外部システム・オフライン可」
   から逸脱する。よって L2 は **hosted / cloud モード**として、LAN 会場モード（name-pick）と
   **共存**させる設計とする（player_id が両モードの橋渡し。sync = ADR-0022 で player_id を跨いで収束）。
   L2 着手時に「ホスティング形態・PII の最小化・トークン保管」を別 ADR で確定する。
5. **staff 認証（ADR-0021 の staff shared token）とは直交**。本 ADR は player 本人の識別/認証のみ。

## Alternatives Considered

- **PIN を今すぐ実装して将来 OAuth に置換** — PIN が throwaway になり、principal 抽象が無いと
  OAuth 移行で API 認可を作り直す。→ 先に principal レイヤと auth_identity モデルを固め、
  PIN/OAuth を additive に。
- **外部 subject を player_id に使う（IdP を採番元に）** — ADR-0004 の「アプリ内採番」に反し、
  IdP 失効・乗り換え・複数 IdP link で破綻。→ player_id は内部不変、外部は auth_identity で紐づけ。
- **OAuth を LAN モードに直接載せる** — オフライン・無外部システム前提を壊す。→ hosted モードとして分離。

## Consequences

- Positive: name-pick → PIN → 外部 IdP が **additive**（player_id・データモデル・エンドポイント契約は
  不変）。LAN モードと hosted モードが player_id で共存可能。将来の会員機能の土台ができる。
- Negative / trade-offs: 外部 IdP は別 deployment（cloud）と PII / セキュリティ設計を要し、現行の
  単純な LAN 運用より重い。L2 着手前に hosting / 個人情報 / トークン保管の ADR が必要。
- Neutral: 当面の実装は L0 のまま。本 ADR は「spec が将来を許容する」設計記録であり、L1/L2 の
  コードは含まない。`player` schema（1.0 frozen）への `pin_hash` / `auth_identity` 追加は **additive**
  （optional field / 別ストア）で frozen 規則内。

## Validation / Follow-up

- [ ] L1（PIN）: `players.json` に additive な `pin_hash`、principal 解決に PIN 検証、player self-service
  write の認可。LAN 内。
- [ ] L2（外部 IdP）: hosting / PII / トークン保管の ADR → `auth_identity` ストア → OIDC 検証 →
  principal 解決の差し替え → 初回 link UX。
- [ ] 着手順は別途決定（本 ADR は方針のみ）。

## Related Files

- `core/player.py` / `core/player_repository.py`（L1 で additive な credential）
- `api/server.py`（principal 解決レイヤ）/ `docs/contracts/shared-ids.md`（player_id 不変条件）
- 将来: `core/auth_identity*.py`（L2, planned）

## Related Tests

- 将来 L1/L2 着手時に追加。

## Related Commits

- 本 ADR と同じ commit（方針記録のみ）

## Supersedes / Superseded by

- Supersedes: —（ISSUE-0019 の「PIN は将来再評価」を方針として具体化。関連: ADR-0004 / ADR-0021 / ISSUE-0019）
- Superseded by: —
