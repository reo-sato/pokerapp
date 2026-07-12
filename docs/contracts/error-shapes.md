# Error shapes contract

front-end が UI 分岐できるよう、core / repository が返す error を **種別（code）で識別** できる
形に固定する。文言（message）は表示用であり、分岐に使わない。

## 原則

- **分岐は `code`、表示は `message`**。front-end は `code` で UI を出し分ける。`message` は
  ローカライズ・改稿されうるので分岐キーにしない。
- **core が source of truth**。error の発生条件（validation 等）は core 側のルール
  （`validation-rules.md`）に従う。front-end は error を作らず受け取るだけ。
- 言語非依存の契約。Python は例外クラス、mobile は reject/Result で表現してよいが `code` は共通。

## 共通 error code（bootstrap）

| code | 意味 | 発生する操作（例） | Python 例外（現状 core） |
|------|------|------------------|------------------------|
| `not_found` | 指定 ID のエンティティが存在しない | get / rename | `PlayerNotFoundError` |
| `empty_display_name` | display_name が空 / 前後空白のみ | create / rename | `EmptyDisplayNameError` |
| `duplicate_display_name` | display_name が既存と完全一致（前後空白除去後） | create / rename | `DuplicateDisplayNameError` |
| `validation_error` | 上記に当てはまらない汎用 validation 失敗（基底） | 各種 | `PlayerValidationError` |

> 現状 `core/player_repository.py` の例外階層:
> `PlayerValidationError`（基底）← `EmptyDisplayNameError` / `DuplicateDisplayNameError` /
> `PlayerNotFoundError`。本 doc の `code` はこの階層と 1:1 対応する。

## error の論理形（cross-app / API 化を見据えた表現）

将来 API / mobile で受け渡す論理形（今回は schema 化までは不要、契約の方向性のみ示す）:

```text
{
  "code": "duplicate_display_name",   // 機械可読・分岐キー（安定）
  "message": "「Alice」は既に存在します。",  // 表示用（可変・ローカライズ対象）
  "field": "display_name"             // 任意: 該当フィールド（フォーム表示用）
}
```

## 拡張ルール

- 新しい error は **code を追加**（additive）。既存 code の意味変更・削除は breaking
  （`versioning-and-freeze.md` §2）。
- 各 model の固有 error（session / ledger / settlement）は対応 phase の freeze 時に本表へ追記する。

### session / seating error code（S2 core 実装済）

`core/session_repository.py` が返す code（additive）。Python 例外階層は
`SessionError`（基底）← 各 code に 1:1 対応する subclass:

| code | 意味 | 発生する操作（例） | Python 例外（core） |
|------|------|------------------|---------------------|
| `not_found` | 指定 `session_id` / hand が存在しない（既存 code を再利用） | get / assign / resolve | `SessionNotFoundError` |
| `already_closed` | closed の session を再度 close / 変更しようとした | close session | `SessionAlreadyClosedError` |
| `session_closed` | closed の session に seat 割り当てしようとした | assign seat | `SessionClosedError` |
| `seat_taken` | 同一 hand 内で席が既に埋まっている | assign seat | `SeatTakenError` |
| `player_already_seated` | 同一 hand 内で player が既に別の席に着いている | assign seat | `PlayerAlreadySeatedError` |
| `unknown_player` | 指定 `player_id` が registry に実在しない | assign seat | `UnknownPlayerError` |
| `invalid_seat` | `seat_no` が範囲外（1..9 外）/ `hand_id` が不正 | assign seat | `InvalidSeatError` |

### ledger / points / settlement error code（S3 core 実装済 — ADR-0016）

`core/ledger_repository.py` が返す code。Python 例外階層は `LedgerError`（基底）← 各 code に
1:1 対応する subclass。共通 `not_found` / `unknown_player` の意味を再利用する（ledger 専用の例外クラスで投げる）:

| code | 意味 | 発生する操作（例） | Python 例外（core） |
|------|------|------------------|---------------------|
| `not_found` | 指定 `entry_id` / `session_id` / settlement が存在しない | reverse / list / settlement | `LedgerNotFoundError` |
| `unknown_player` | 指定 `player_id` が registry に実在しない | add entry / grant | `UnknownPlayerError` |
| `invalid_amount` | 金額・符号・order 明細・reversal 要求が不正 | add entry / grant | `InvalidAmountError` |
| `entry_fee_requires_cash` | `kind=entry_fee` に point を充当しようとした（cash only, rule 1） | add entry | `EntryFeeRequiresCashError` |
| `insufficient_points` | spend が point 残高を割り込む（不足分は cash 補完, rule 3） | add entry（point 充当） | `InsufficientPointsError` |
| `duplicate_grant` | 同一 `idempotency_key` の grant が既に存在 | grant points | `DuplicateGrantError` |
| `session_not_closed` | open session を settlement 確定しようとした | commit settlement | `SessionNotClosedError` |
| `already_settled` | 確定済 settlement を再確定しようとした | commit settlement | `AlreadySettledError` |

> `payment_status` の `unpaid↔paid` 操作は訂正として許容（error にしない）。kind / reason / status の
> enum 外指定は呼び出し側のバグとして `ValueError`（front-end は固定 enum から渡すため通常到達しない）。
> settlement schema の `1.0` freeze は #5（S4）。

### viewer API / order-request error code（M1/M5 — ADR-0017/0018）

viewer API（`api/server.py`）+ 注文リクエスト core（`core/order_request_repository.py`）が返す
code（additive）。Python 例外階層は `OrderRequestError`（基底）← 各 code に対応する subclass。
viewer API は menu 照合（`unknown_item`）と read-only モード（`orders_unavailable`）を境界で扱う:

| code | 意味 | 発生する操作（例） | Python 例外 / 由来 | HTTP |
|------|------|------------------|---------------------|------|
| `not_found` | unknown player / session / hand / request（既存 code を再利用）。**cancel では他人の request / session 不一致も not_found**（存在を漏らさない, ADR-0045） | 各 GET / confirm / cancel | `*NotFoundError` | 404 |
| `unknown_player` | 指定 `player_id` が registry に実在しない | create request | `OrderUnknownPlayerError` | 404 |
| `session_closed` | closed session に注文 / 確定しようとした | create / confirm request | `OrderSessionClosedError` | 409 |
| `invalid_quantity` | quantity 範囲外（1..99 外）/ item_name 空・過長 / note 過長 | create request | `InvalidOrderRequestError` | 400 |
| `unknown_item` | menu に無い品名（menu 照合は API 境界） | POST order-request | API 境界（menu master） | 400 |
| `already_resolved` | 終端（confirmed / rejected / cancelled）済の request を再解決しようとした | confirm / reject / cancel request | `AlreadyResolvedError` | 409 |
| `orders_unavailable` | read-only モード（単独 `--viewer-api`）で注文 write を受けた | POST order-request / staff write | API 境界（orders_writable=False） | 503 |

### staff write API error code（S5 — ADR-0021）

スタッフ会計 write API（`/api/staff/...`）の認可で使う code（additive）。ledger / settlement の
実エラーは上の **ledger / points / settlement** セクションの code をそのまま再利用し、注文確定・却下は
**viewer API / order-request** セクションの code を再利用する（本節は認可固有の 2 code のみ）:

| code | 意味 | 発生する操作（例） | 由来 | HTTP |
|------|------|------------------|------|------|
| `unauthorized` | `Authorization: Bearer <token>` が欠落 / staff_token と不一致 | `/api/staff/...` 全般 | API 境界（`_staff_guard`） | 401 |
| `staff_writes_disabled` | `viewer_api.staff_token` 未設定（staff API が運用で無効） | `/api/staff/...` 全般 | API 境界（`_staff_guard`） | 403 |

- staff write を write 非所有プロセス（単独 `--viewer-api`）が受けた場合は既存
  `orders_unavailable`(503) を再利用する（単一書き手, ADR-0020）。

### player 認証 / L1 PIN error code（ADR-0027）

player 本人の PIN ログイン（`POST /api/auth/login`）/ PIN 設定（`POST /api/players/{id}/pin`）/
self-write の principal ガードで使う code（additive）。`unauthorized`(401) / `not_found`(404) は既存
code を再利用する。principal 解決は API 境界（`_resolve_player_principal` / `_require_player`）と
`core/player_credential_repository.py`（lockout）が source:

| code | 意味 | 発生する操作（例） | 由来 | HTTP |
|------|------|------------------|------|------|
| `player_auth_disabled` | `viewer_api.player_auth=off`（player 認証が運用で無効） | login / pin 設定 | API 境界 | 403 |
| `invalid_pin` | PIN が不一致 | login | API 境界（`verify_pin`=False） | 401 |
| `pin_locked` | 連続失敗で lockout 中（`pin_max_attempts` 超過） | login / pin 変更時の現 PIN 照合 | `PinLockedError` | 429 |
| `pin_too_short` | PIN が `pin_min_length` 未満 | pin 設定 | `PinTooShortError` | 400 |
| `unauthorized` | 本人トークン欠落 / 初回設定に staff token 必要 / 現 PIN 不正（既存 code を再利用） | self-write / pin 設定 | API 境界 | 401 |
| `forbidden` | 本人トークンの player_id が path の player_id と不一致 | self-write | API 境界（principal != path） | 403 |

- `player_auth` 別の挙動: `off`=name-pick（gate なし, 後方互換）/ `optional`=PIN 登録済 player の
  write のみ要求 / `required`=全 player write に本人トークン要求。read は対象外（本人トークン不要）。

### player merge error code（ADR-0030）

staff merge（`POST /api/staff/players/merge`）で使う code（additive）。`not_found`(404) は既存 code を
再利用（unknown survivor / absorbed）:

| code | 意味 | 発生する操作 | Python 例外 | HTTP |
|------|------|------------|-------------|------|
| `invalid_merge` | 自己 merge / サイクルになる merge | merge | `PlayerMergeError` | 400 |

### L2 外部 IdP error code（ADR-0031）

`POST /api/auth/{provider}/exchange`（外部 IdP の認可コード交換）で使う code（additive）。実 provider の
HTTP 実装は実環境タスクだが、エラー契約はここで固定する:

| code | 意味 | 発生する操作 | 由来 | HTTP |
|------|------|------------|------|------|
| `unknown_provider` | provider 未構成（実 IdP 未構築 / LAN 既定で OIDC 無効） | exchange | API 境界 | 404 |
| `invalid_idp_code` | 認可コードの検証失敗（期限切れ / 改竄 / 未登録） | exchange | `OidcError` | 401 |
| `identity_conflict` | 同一 (provider, subject) を別 player に link しようとした | link（内部） | `AuthIdentityConflictError` | 409 |

### ハンド訂正 error code（ADR-0036）

`POST /api/staff/sessions/{sid}/hands/{hid}/corrections`（staff write）で使う code（additive）。
`not_found`(404, unknown hand) は既存再利用:

| code | 意味 | 由来 | HTTP |
|------|------|------|------|
| `invalid_correction` | field/value/action_index が不正（範囲外 index・未知 field・型不正） | `HandCorrectionError` / API 境界 | 400 |
