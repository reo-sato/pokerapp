# Viewer API contract（M1, draft 0.x — 未 freeze）

player 向け参照 front-end（WS3, Expo）が消費する **読み取り専用 HTTP API** の契約
（ADR-0013。S5 cross-app boundary の read-only サブセットの前倒し）。

- server: 運営 PC 上の `api/server.py`（FastAPI, `[api]` extra）。起動は 2 形態:
  - `python main.py --viewer-api` = **read-only モード**（注文 POST は 503 `orders_unavailable`）。
  - `python main.py --ledger`（`viewer_api.enabled=true`）= スタッフ会計画面に **in-process 組み込み**。
    注文 write が有効になる（単一プロセス所有, ADR-0015 §3）。
- bind 既定: `127.0.0.1:8788`（無認証のため。LAN 公開は `config.viewer_api.bind_host` を明示変更。
  rfid receiver と同じ前例）。プライバシーモデルは **v1 = name-pick で確定**（ISSUE-0013 Fixed,
  ADR-0015。注文はスタッフ確定を挟むため対面で検証される）。
- **M1 は GET のみ／M5 で注文リクエストの write を追加**（ledger への直接 write は無い —
  `ledger.md` § 注文リクエスト）。
- version: **0.x draft**。session 系 model（session / seat_assignment / hand_ref）が draft（0.x,
  ISSUE-0005 gate）のままなので、本 API 契約の freeze もそれに従う。response の構成要素は
  既存 schema を `$id` で参照し、本 doc は envelope（包み方）のみを定義する。

## read model の規則（ADR-0013 §Decision 6）

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
| GET | `/api/players/{player_id}/sessions/{session_id}/ledger` | `{"entries": [ledger_entry, ...], "summary": {buy_in_total, order_total, adjustment_total, total_due}}`（M4/S3a additive。ledger_entry = `ledger_entry.schema.json` v0.x、summary は `ledger.md` の中間集計 = 確定値ではない） | 404 `not_found`（unknown player / unknown session） |
| GET | `/api/menu` | `{"items": [{"item_name", "unit_amount"}, ...]}`（menu.json master, M5。空なら `[]`） | — |
| GET | `/api/players/{player_id}/sessions/{session_id}/order-requests` | `{"requests": [order_request, ...]}`（自分のもののみ、requested_at 順。`order_request.schema.json` v0.x） | 404 `not_found` |
| POST | `/api/players/{player_id}/sessions/{session_id}/order-requests` | body `{"item_name", "quantity", "note"?}` → 201 + 作成された order_request（status=pending。**ledger には書かれない** — スタッフ確定で初めて記帳, ADR-0015） | 404 `not_found` / 400 `invalid_quantity` / 400 `unknown_item`（menu 外） / 409 `session_closed` / 503 `orders_unavailable`（read-only モード） |

備考:

- `/api/sessions/{session_id}/hands/{hand_id}` は hand log ファイルを直接参照するため、
  session レイヤ未登録の legacy session_id（timestamp 形式）でも log が存在すれば返す。
- `player_session_summary` は session（0.x）のフィールド + `hands_played`（着席 hand 数）の
  envelope。schema は `schemas/player_session_summary.schema.json`、fixtures は
  `fixtures/player_session_summary/`（M2 mock の初期データ oracle）。

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
