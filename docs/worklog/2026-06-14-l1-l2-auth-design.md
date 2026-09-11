# Worklog: player 認証 L1 PIN / L2 外部 IdP の詳細設計（ADR-0027 / ADR-0028）

## Date

2026-06-14

## Scope / Task

ADR-0025（player アイデンティティ/認証の進化方針 = 方針記録のみ）を受けて、ユーザー依頼により
**L1（per-player PIN）と L2（外部 IdP: LINE/Google OIDC）を実装可能なレベルの詳細設計**まで具体化する。
**設計タスク**であり、本コミットにコードは含まない（design ADR + docs のみ）。

## Goal

- L1/L2 を「読めば実装できる」粒度の ADR にする。
- ADR-0004 の不変条件（player_id はアプリ内採番・不変・外部非依存）を破らない設計にする。
- L0（name-pick）を壊さない後方互換と、L1→L2 が同一 principal/トークン契約に load する合流点を示す。

## Changed Files

- `docs/adr/0027-l1-per-player-pin-auth-design.md`（新規, Proposed）: PIN credential を node-local
  `player_credentials.json` に分離（PBKDF2-HMAC-SHA256 + per-player lockout）。検証後に stateless 署名
  トークン（HMAC, `viewer_api.player_token_secret`）を発行し、**player principal 解決レイヤ**で
  self-write を認可。config `viewer_api.player_auth`（off/optional/required, 既定 off）。auth
  エンドポイント（login / pin set-change）と error code を additive 定義。
- `docs/adr/0028-l2-external-idp-oidc-auth-design.md`（新規, Proposed）: `auth_identity`
  `(provider, subject)→player_id`（多対一）。OIDC Authorization Code フロー（state/nonce/PKCE +
  JWKS 検証）→ L1 と同形 player トークン発行。hosted モードで LAN 会場モードと player_id + sync 共存。
  初回 link = 新規採番 + staff link/merge（player merge は依存・前提課題）。PII 最小化（sub のみ保存、
  IdP トークン非保存）。運用面は実装時 ops ADR に委譲。
- `docs/decision-log.md`: ADR-0027 / ADR-0028 行を追加。
- `docs/issues/0019-player-viewer-privacy-model.md`: ADR-0027/0028 の詳細設計反映の Update 節を追加。
- `CLAUDE.md`: 残作業 #7（player 本人確認の進化）を ADR-0027/0028 参照に更新。
- `CHANGELOG.md`: Docs 節を追加。

## Key Design Decisions（精緻化した点）

- **PIN を players.json に持たない**（ADR-0025 素描からの変更）: players.json は viewer API が serialize
  して返し、かつ sync snapshot 対象（players/sessions/ledger/orders）。pin_hash を載せると read API 漏れ /
  peer 伝播 / 1.0 frozen schema 変更の 3 リスク。→ node-local 別ストアに分離（read/sync 非対象）。
- **principal 解決レイヤ + stateless 署名トークン**を L1/L2 共通の合流点に。エンドポイント契約を認証方式に
  非依存にする（L0→L1→L2 が additive）。
- **player_id を外部 sub から導出しない**。auth_identity は単なる lookup（IdP 失効/乗り換え/複数 link で
  player_id を動かさない）。
- **既定 off** で挙動不変（ISSUE-0019 の name-pick 出荷と矛盾しない）。

## Expected / Implemented Behavior

- 設計タスクのため挙動変更なし（コードなし）。両 ADR は Proposed（実装は別タスク）。

## Test Results

- `python -m pytest tests/ --ignore=tests/test_vision.py -q` — **530 passed, 0 skipped**（docs のみ変更、
  コード不変を確認）。
- `ruff check .` — clean。

## Mismatches Found During Testing

- なし（コード変更なし）。

## Remaining Gaps / Out-of-Scope（実装着手時の依存）

- L1: `core/player_credential_repository.py` + principal/トークン実装 + `api/server.py` 結線 +
  config + `error-shapes.md` auth 節 + テスト。
- L2: 運用面 ops ADR（hosting/secret/PII/法令）と **player merge フロー（現状 scope 外）**が前提。
  `core/auth_identity*.py` + OIDC callback + モック IdP テスト。
- 着手順は別途決定（需要が固まったら L1 から）。

## Related ADRs / Issues

- ADR-0027（L1）/ ADR-0028（L2）/ ADR-0025（方針）/ ADR-0004（player_id 不変）/ ADR-0021（staff token と
  直交）/ ADR-0022（sync は player_id で収束）/ ISSUE-0019

## Related Commits

- 本 commit
