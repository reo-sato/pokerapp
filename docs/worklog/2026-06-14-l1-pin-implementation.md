# Worklog: L1 per-player PIN 認証の実装（ADR-0027）

## Date

2026-06-14

## Scope / Task

ADR-0027（L1 PIN の詳細設計）を実装する。player の self-write（注文 POST 等）を PIN ログインで本人認証
できる additive レイヤを追加。既定は後方互換（name-pick）。L2（外部 IdP, ADR-0028）は hosting/外部 IdP/
player merge が前提のため本タスク対象外。

## Goal

- player principal 解決レイヤ + stateless 署名トークンを導入し、`player_auth=optional|required` で
  self-write を本人認証する。`off`（既定）で挙動不変。
- PIN を players.json / read API / sync から分離した node-local ストアに保持（漏洩・伝播・schema 変更を避ける）。
- ADR-0004（player_id 不変）・player schema 1.0 frozen・sync snapshot（4 パス）を一切触らない。

## Changed Files

- `core/auth_token.py`（新規）: `issue_player_token` / `verify_player_token`。形式
  `v1.<player_id>.<exp>.<hmac>`、HMAC-SHA256、now 注入で決定的。
- `core/player_credential_repository.py`（新規）: `PlayerCredentialRepository`。PBKDF2-HMAC-SHA256 +
  per-PIN salt（平文非保持）、`set_pin`/`verify_pin`/`has_pin`、per-player lockout（`failed_attempts`/
  `locked_until`、now 注入）、アトミックリネーム永続、`reload`。`PinTooShortError`/`PinLockedError`。
- `api/server.py`: `create_app` に `credential_repo`/`player_auth`/`player_token_secret`/
  `player_token_ttl_sec`/`pin_self_enroll` を additive。principal レイヤ `_resolve_player_principal` /
  `_require_player`。`POST /api/auth/login`（PIN→トークン）/ `POST /api/players/{id}/pin`（初回=staff or
  self-enroll、変更=現 PIN or staff reset）。注文 POST に `_require_player` gate（`request: Request` 追加）。
  `auth_kwargs_from_config(api_cfg)` ヘルパ（--ledger / --viewer-api で共通）。
- `api/client.py`: `ViewerApiClient(player_token=...)` + `_player_headers` + `login` / `set_pin`。
  `create_order_request` に player token を付与。
- `main.py`: `run_ledger_view` の in-process API に `auth_kwargs_from_config` を結線。
- `config_default.json`: `viewer_api` に `player_auth`/`player_token_secret`/`player_token_ttl_sec`/
  `pin_self_enroll`/`pin_min_length`/`pin_max_attempts`/`pin_lockout_sec` + 説明コメント。
- `.gitignore`: `player_credentials.json`。
- tests（新規）: `tests/test_auth_token.py` / `tests/test_player_credential_repository.py` /
  `tests/test_viewer_api_auth.py`。
- docs: `docs/adr/0027-...md`（Status→Accepted/実装済、Validation チェック）、`docs/decision-log.md`、
  `docs/issues/0019-...md`、`docs/contracts/error-shapes.md`（auth 節）、`docs/contracts/viewer-api.md`
  （player 認証 / L1 PIN 節）、`CLAUDE.md`、`CHANGELOG.md`。

## Key Design Decisions（実装で確定）

- **PIN は node-local 別ストア**（ADR-0027 D1 どおり）。players.json は read API serialize + sync snapshot
  対象のため、credential を入れると漏洩・伝播・schema 変更の 3 リスク。→ `player_credentials.json` を read/
  sync 非対象に。
- **stateless 署名トークン**（HMAC、サーバ側セッションストアなし）。L2 も同形を発行する合流点。
- **principal != path player_id は 403 `forbidden`**、トークン欠落は 401 `unauthorized`。
- **secret 未設定なら ephemeral**（起動ごと、再起動でトークン失効）。固定したい場合のみ config。

## Expected / Implemented Behavior

- `player_auth="off"`（既定）: 注文 POST はトークン不要、login は 403 `player_auth_disabled`（挙動不変）。
- `optional`: PIN 登録済 player の write のみトークン要求（未登録は name-pick 継続）。
- `required`: 全 player write にトークン要求。`login` 成功 → token、以後 self-write 可。
- lockout: `pin_max_attempts` 連続失敗で `pin_lockout_sec` 秒 429 `pin_locked`。

## Test Results

- `pytest tests/test_auth_token.py tests/test_player_credential_repository.py tests/test_viewer_api_auth.py -q`
  — 27 passed。
- `pytest tests/ --ignore=tests/test_vision.py -q` — **557 passed, 0 skipped**（既存 530 + 27、後方互換 OK）。
- `ruff check .` — clean。

## Mismatches Found During Testing

- なし（既定 off で既存テスト全緑、追加テストも一発で緑）。

## Remaining Gaps / Out-of-Scope

- read の per-player 保護（現状 read は name-pick のまま）/ GUI からの PIN 設定 UI / player 削除時の
  credential 連動削除 = L1 follow-up。
- L2 外部 IdP（ADR-0028）= 設計済・未実装（運用 ADR + player merge が前提）。

## Related ADRs / Issues

- ADR-0027（本件 L1）/ ADR-0028（L2 設計）/ ADR-0025（方針）/ ADR-0004（player_id 不変）/
  ADR-0021（staff token と直交）/ ISSUE-0019

## Related Commits

- 本 commit
