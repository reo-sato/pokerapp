# Viewer API contract（M1/M5, draft 0.x — 未 freeze）

player 向け参照 front-end（WS3, Expo）が消費する **読み取り専用 HTTP API** の契約
（ADR-0017。S5 cross-app boundary の read-only サブセットの前倒し）。

- server: 運営 PC 上の `api/server.py`（FastAPI, `[api]` extra）。起動は 2 形態:
  - `python main.py --viewer-api` = **read-only モード**（注文 POST は 503 `orders_unavailable`）。
  - `python main.py --ledger`（`viewer_api.enabled=true`）= スタッフ会計画面に **in-process 組み込み**。
    注文 write が有効になる（単一プロセス所有, ADR-0018 §3）。
- bind 既定: `127.0.0.1:8788`（無認証のため。LAN 公開は `config.viewer_api.bind_host` を明示変更。
  rfid receiver と同じ前例）。プライバシーモデルは **v1 = name-pick で確定**（ISSUE-0019 Fixed,
  ADR-0018。注文はスタッフ確定を挟むため対面で検証される）。
- **M1 は GET のみ／M5 で注文リクエストの write を追加**（ledger への直接 write は無い —
  本 doc § 注文リクエスト / verify-v1 ledger は `ledger-overview.md` / `ledger-schema.md`）。
- version: **0.x draft**。session 系 model（session / seat_assignment / hand_ref）が draft（0.x,
  ISSUE-0005 gate）のままなので、本 API 契約の freeze もそれに従う。response の構成要素は
  既存 schema を `$id` で参照し、本 doc は envelope（包み方）のみを定義する。

## read model の規則（ADR-0017 §Decision 6）

- 「player X のハンド」は `sessions.json` の seat_assignment（ADR-0008 の source of truth）から
  導出し、hand log（`logs/{session_id}.json`）を `(session_id, hand_id)` 複合キーで join する。
- hand log 側 `players[].player_id` は best-effort（補助情報）であり、帰属判定に使わない。
- seat assignment は存在するが対応する hand log が無い場合（E3 前・log 欠落）、その hand は
  一覧から **静かに除外** する（gracefully-empty）。エラーにしない。

## endpoints

| Method | Path | 200 response | error |
|--------|------|--------------|-------|
| GET | `/api/health` | `{"status": "ok", "version": "<app version>"}` | — |
| GET | `/api/players` | `{"players": [player, ...]}`（作成順。player = `player.schema.json` v1.0） | — |
| GET | `/api/players/{player_id}` | player | 404 `not_found` |
| GET | `/api/players/{player_id}/sessions` | `{"sessions": [player_session_summary, ...]}`（player が 1 hand 以上着席した session のみ。`player_session_summary.schema.json` v0.x） | 404 `not_found`（unknown player） |
| GET | `/api/players/{player_id}/sessions/{session_id}/hands` | `{"hands": [hand, ...]}`（player が着席していた hand の HandSummary。hand = `hand.schema.json` v1.0。hand_id 昇順） | 404 `not_found`（unknown player / unknown session） |
| GET | `/api/sessions/{session_id}/hands/{hand_id}` | hand（HandSummary 全体） | 404 `not_found`（log 不在 / hand 不在） |
| GET | `/api/players/{player_id}/sessions/{session_id}/ledger` | `{"entries": [ledger_entry, ...], "summary": {cash_in_total, order_total, entry_fee, point_spent_total, point_credited_total, net_due_to_store, settled, payment_status, settled_at, paid_amount}}`（ledger_entry = `ledger_entry.schema.json` v0.x。totals は `SessionSettlement` を当該 player に絞った値。`settled`(bool)/`payment_status`/`settled_at` は確定状態 = `list_settlements` 由来で、未確定なら settled=false・payment_status=null。`payment_status` は `partial` を取り得る・`paid_amount`(累計受領額, 未確定は 0) も additive（ADR-0023）。S4 mobile 表示用 additive） | 404 `not_found`（unknown player / unknown session） |
| GET | `/api/menu` | `{"items": [{"item_name", "unit_amount"}, ...]}`（menu.json master, M5。空なら `[]`） | — |
| GET | `/api/players/{player_id}/sessions/{session_id}/order-requests` | `{"requests": [order_request, ...]}`（自分のもののみ、requested_at 順。`order_request.schema.json` v0.x） | 404 `not_found` |
| POST | `/api/players/{player_id}/sessions/{session_id}/order-requests` | body `{"item_name", "quantity", "note"?}` → 201 + 作成された order_request（status=pending。**ledger には書かれない** — スタッフ確定で初めて記帳, ADR-0018） | 404 `not_found` / 400 `invalid_quantity` / 400 `unknown_item`（menu 外） / 409 `session_closed` / 503 `orders_unavailable`（read-only モード） |

備考:

- `/api/sessions/{session_id}/hands/{hand_id}` は hand log ファイルを直接参照するため、
  session レイヤ未登録の legacy session_id（timestamp 形式）でも log が存在すれば返す。
- `player_session_summary` は session（0.x）のフィールド + `hands_played`（着席 hand 数）の
  envelope。schema は `schemas/player_session_summary.schema.json`、fixtures は
  `fixtures/player_session_summary/`（M2 mock の初期データ oracle）。

## 注文リクエスト（M5, ADR-0018）

player はスマホから **order_request**（`order_request.schema.json` v0.x）を POST する。これは
**ledger には書かれず** pending として記録されるだけで、スタッフが会計画面（`--ledger`）で
**確定 (confirm)** したときに初めて `ledger_entry`（kind=order, verify-v1 ledger）が作られ、
`order_request.ledger_entry_id` がリンクされる（staff-in-the-loop）。却下 (reject) は ledger に
何も書かない。

- **menu master**: `menu.json`（コミット済みサンプル、店側で編集）。POST 時に menu 外の品名は
  400 `unknown_item`。確定時の単価は menu から prefill（スタッフ上書き可）で、ledger entry の
  `cash_amount = unit_amount × quantity`、`order = {item_name, unit_amount, quantity}`。
- **単一プロセス所有**: `order_requests.json` の write は viewer API を in-process で抱えた
  `--ledger` プロセスのみ（`viewer_api.enabled=true`）。単独 `--viewer-api` は read-only
  （POST 503 `orders_unavailable`）。
- **closed session への確定**は不可（confirm 時に 409 `session_closed`。verify-v1 ledger は
  closed を拒否しないため、注文確定の closed ガードは order-request 層が担う）。
- 状態遷移は pending → confirmed | rejected のみ（再解決は 409 `already_resolved`）。

## player 認証 / L1 PIN（ADR-0027）

player 本人の self-write（注文 POST 等）を **PIN ログイン**で本人認証する additive レイヤ。
既定は **`viewer_api.player_auth=off`**（= v1 の name-pick / 無認証, ISSUE-0019）で**挙動不変**。
`optional` / `required` で有効化する。staff token（ADR-0021）とは**直交**（staff = 店側、PIN = player 本人）。

- **principal トークン**: PIN 検証成功で stateless 署名トークン（`v1.<player_id>.<exp>.<hmac>`,
  `core/auth_token.py`）を発行。client は self-write 時に `Authorization: Bearer <player token>` で送る。
  サーバはこれを player_id に解決し（`_resolve_player_principal`）、「principal == path の player_id」を要求。
- **モード**: `optional`=PIN 登録済 player の write のみトークン要求（未登録は name-pick 継続）/
  `required`=全 player write にトークン要求。**read は対象外**（トークン不要）。
- **PIN 保存**: node-local `player_credentials.json`（PBKDF2 + per-player lockout）。**read API / sync
  非対象**（player schema・players.json・sync snapshot は不変）。

### endpoints

| Method | Path | auth | body | 200 response |
|--------|------|------|------|--------------|
| POST | `/api/auth/login` | no（PIN 自体が credential） | `{"player_id", "pin"}` | `{"token", "expires_at", "player_id"}`。失敗 401 `invalid_pin` / lockout 429 `pin_locked` / off 403 `player_auth_disabled` / unknown 404 `not_found` |
| POST | `/api/players/{player_id}/pin` | 初回: staff token **or** `pin_self_enroll`。変更: 現 PIN **or** staff token | `{"pin", "current_pin"?}` | `{"player_id", "pin_set": true}`。401 `unauthorized` / 400 `pin_too_short` / 403 `player_auth_disabled` |

`optional`/`required` 有効時、注文 POST（上表）の error に **401 `unauthorized`（トークン欠落）/
403 `forbidden`（principal != path player_id）** が追加される。code は `error-shapes.md` の
「player 認証 / L1 PIN」節と 1:1。

## staff write API（ADR-0021）

別端末のスタッフが会計をリモート操作するための **staff 専用** エンドポイント群。player API
（name-pick / 無認証）とは分離し、`/api/staff/` 配下に置く。**すべて staff shared token 必須**。

### 認証（staff shared token）

- config `viewer_api.staff_token` を設定すると有効。リクエストは
  `Authorization: Bearer <staff_token>` を付ける。
- token 未設定（空）→ **403 `staff_writes_disabled`**（運用で有効化していない）。
- token 欠落 / 不一致 → **401 `unauthorized`**。
- write 系（need_write）は **write 所有プロセス（`--ledger`, `viewer_api.enabled=true`）のみ**。
  単独 `--viewer-api`（read-only）では write は **503 `orders_unavailable`**（単一書き手, ADR-0020）。
  staff *read*（settlement / 注文 queue）は token があれば read-only プロセスでも可。
- **player read / 注文 POST は従来どおり無認証**（name-pick, ISSUE-0019）。本節は staff write のみ。

### endpoints

| method | path | write | body | 返り値 |
|--------|------|-------|------|--------|
| GET  | `/api/staff/buyin-presets` | no | — | `{"presets": [int, ...]}`（config `ledger.buyin_presets` 由来。buy-in 金額メニュー, ADR-0026） |
| POST | `/api/staff/players/merge` | yes | `{"survivor_id", "absorbed_id"}` | absorbed を survivor に統合（player merge, ADR-0030）。`{"survivor_id", "absorbed_id", "merged_into", "merged_at"}`。404 `not_found` / 400 `invalid_merge`（自己 merge / サイクル）。registry 書き込みのため write 所有プロセスのみ |
| GET  | `/api/staff/sessions/{session_id}/settlement` | no | — | `{"settlements": [session_settlement, ...]}`（compute_settlement, speculative） |
| GET  | `/api/staff/sessions/{session_id}/order-requests?status=` | no | — | `{"requests": [order_request, ...]}`（全 player の queue。status query 任意） |
| POST | `/api/staff/sessions/{session_id}/ledger-entries` | yes | `{player_id, kind, cash_amount?, point_amount?, note?, hand_id?, order?}` | 201 `ledger_entry` |
| POST | `/api/staff/sessions/{session_id}/settlement/commit` | yes | — | `{"settlements": [...]}` |
| PUT  | `/api/staff/sessions/{session_id}/players/{player_id}/payment-status` | yes | `{status: "paid"\|"unpaid"}` | 更新後 `session_settlement`（shortcut。paid=全額受領 / unpaid=受領 0。partial は不可） |
| PUT  | `/api/staff/sessions/{session_id}/players/{player_id}/payment` | yes | `{paid_amount}`（>=0, 累計受領額） | 更新後 `session_settlement`（partial-paid, ADR-0023。`payment_status` を導出） |
| POST | `/api/staff/order-requests/{request_id}/confirm` | yes | `{unit_amount}` | 更新後 `order_request`（ledger order entry をリンク） |
| POST | `/api/staff/order-requests/{request_id}/reject` | yes | — | 更新後 `order_request` |

#### 会計の不足分（ADR-0036 §A）

| method | path | write | body | 返り値 |
|--------|------|-------|------|--------|
| GET  | `/api/staff/sessions/{session_id}/ledger-entries` | no | — | `{"entries": [ledger_entry, ...]}`（挿入順。reversal UI が取消対象を選ぶ read。lenient: unknown session は空 list） |
| POST | `/api/staff/ledger-entries/{entry_id}/reverse` | yes | — | reversal の `ledger_entry`（append-only。`reverses_entry_id` 付き）。404 `not_found` / 400 `invalid_amount` |
| POST | `/api/staff/players/{player_id}/point-grants` | yes | `{delta_points, reason?, session_id?, idempotency_key?}` | `point_ledger_entry`。404 `unknown_player` / 400 `invalid_amount` / 409 `duplicate_grant` |

#### session / 座席 / player ライフサイクル（ADR-0036 §B）

| method | path | write | body | 返り値 |
|--------|------|-------|------|--------|
| GET  | `/api/staff/sessions` | no | — | `{"sessions": [session, ...]}`（全 session, staff 卓選択用） |
| POST | `/api/staff/sessions` | yes | `{label?, blinds?}` | 201 `session`（UUID4 採番, ADR-0007） |
| POST | `/api/staff/sessions/{session_id}/close` | yes | — | 更新後 `session`。404 `not_found` / 409 `already_closed` |
| GET  | `/api/staff/sessions/{session_id}/seating` | no | — | `{"seating": [seat_assignment, ...], "hand_ids": [int, ...]}`（最新 hand 由来の現在 seating + 記録済 hand_id） |
| PUT  | `/api/staff/sessions/{session_id}/hands/{hand_id}/seats` | yes | `{assignments: [{seat_no, player_id}, ...]}` | `{"assignments": [seat_assignment, ...]}`（append。conflict は session error） |
| GET  | `/api/staff/players` | no | — | `{"players": [player, ...]}`（canonical, ADR-0030） |
| POST | `/api/staff/players` | yes | `{display_name}` | 201 `player`。400 `empty_display_name` / `duplicate_display_name` |
| PUT  | `/api/staff/players/{player_id}` | yes | `{display_name}` | 更新後 `player`。404 `not_found` / 400 `empty_display_name` / `duplicate_display_name` |

### error code（再利用 + 新規）

ledger / settlement の error は `error-shapes.md` の ledger セクションを **再利用**:
`not_found`(404) / `unknown_player`(404) / `invalid_amount`(400) / `entry_fee_requires_cash`(400) /
`insufficient_points`(400) / `duplicate_grant`(409) / `session_not_closed`(409) / `already_settled`(409)。
注文確定・却下は order セクションの `session_closed`(409) / `already_resolved`(409) /
`invalid_quantity`(400) / `not_found`(404)。session / 座席（ADR-0036 §B）は session セクションの
`not_found`(404) / `already_closed`(409) / `session_closed`(409) / `seat_taken`(409) /
`player_already_seated`(409) / `unknown_player`(404) / `invalid_seat`(400)、player registry は
`empty_display_name`(400) / `duplicate_display_name`(400) を再利用。新規 code は認可の
**`unauthorized`(401)** / **`staff_writes_disabled`(403)**、write 所有外は既存 `orders_unavailable`(503)。

## sync API（ADR-0022）

複数の運営ノード（LAN）が全ストアのレプリカを **state-based merge** で相互最新化するための
staff-only エンドポイント。同期は **on-demand pull + peer-to-peer merge**（中央権威なし・
auto-sync なし）。マージは `core/sync.py` の純粋関数で **可換・結合・冪等**（UUID union + 単調
フィールド解決）なので、どのノードがどの順で何度マージしても同じ状態に収束する。

### endpoints

| method | path | write | body | 返り値 |
|--------|------|-------|------|--------|
| GET  | `/api/staff/sync/snapshot` | no | — | このノードの全レコード snapshot（下記 shape） |
| POST | `/api/staff/sync/merge` | yes | peer snapshot dict | merge summary（new-or-changed カウント） |

認可は staff write API と同じ（`Authorization: Bearer <viewer_api.staff_token>`）。`snapshot`
は read（read-only `--viewer-api` でも可）、`merge` は **write 所有プロセス（`--ledger`,
viewer_api.enabled）のみ** が受理（read-only は 503 `orders_unavailable`、単一書き手 → 収束マージ
への移行点）。token 未設定 → 403 `staff_writes_disabled` / 不一致 → 401 `unauthorized`。

### snapshot shape

```json
{
  "players":        [player, ...],
  "sessions":       [session（full nested = hands/seats 込み）, ...],
  "ledger_entries": [ledger_entry, ...],
  "point_entries":  [point_ledger_entry, ...],
  "settlements":    [session_settlement, ...],
  "order_requests": [order_request, ...]
}
```

各レコードは対応モデルの `to_dict` 形そのまま（snapshot は別ノードの同型ストアにそのまま merge できる）。

### merge semantics（per-store, ADR-0022 §Decision.2）

- **players**（key `player_id`）: create-only union。既存 id は local 保持（rename は v1 非伝播）。
- **ledger_entries / point_entries**（key `entry_id`）: union（append-only ⇒ 衝突なし）。
- **order_requests**（key `request_id`）: union + status 解決。pending < {confirmed, rejected}。
  一方終端・他方 pending → 終端採用。両終端で異なる（confirmed vs rejected）→ **confirmed 優先**。
  両 confirmed → `resolved_at` 早い方。
- **settlements**（key `(session_id, player_id)`）: committed > uncommitted。両 committed なら
  **paid_amount の max**（partial-paid 対応, ADR-0024。payment_status は導出）、`settled_at` 早い方の値を保持。
- **sessions**（key `session_id`）: union。`status` closed > open（`ended_at` は closed 側）、
  `label`/`blinds` local 優先。入れ子 `hands`（key hand_id）union、`seats`（key seat_no）union
  で同一 seat_no 衝突は local 優先。
- **merge summary**: `{"players_added", "sessions_added", "ledger_entries_added",
  "point_entries_added", "settlements_added", "order_requests_added"}`（new-or-changed のカウント）。

Python client: `ViewerApiClient.pull_sync_snapshot()` / `push_sync_merge(snapshot)` /
`sync_bidirectional(peer)`（pull+merge を双方向に行い 2 ノードを収束させる）。収束 round-trip test
= `tests/test_sync.py`（純粋）/ `tests/test_viewer_api_sync.py`（HTTP 2 ノード）。

## error 形

`error-shapes.md` の論理形をそのまま HTTP body にする（分岐は `code`、表示は `message`）:

```json
{"code": "not_found", "message": "player_id=... は存在しません。"}
```

- 404 = `not_found`（player / session / hand のいずれでも同一 code。既存 code の再利用）。
- 422（FastAPI の validation error）は M1 では契約対象外（path param は文字列/整数のみ）。

## 拡張ルール

- endpoint / response field の追加は additive。既存 field の削除・意味変更は breaking
  （`versioning-and-freeze.md` §2 に従い ADR を要求）。
- 将来の OpenAPI 自動生成（FastAPI `/openapi.json`）は本 doc と並ぶ参照物だが、
  **契約の source は本 doc + schemas/** とする（README の single source 原則）。
