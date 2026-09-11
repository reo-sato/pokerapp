# ADR-0027: L1 per-player PIN 認証の詳細設計（player principal 解決レイヤ）

## Status

Accepted（**実装済 2026-06-14**。設計どおり `core/auth_token.py` /
`core/player_credential_repository.py` + `api/server.py` の principal レイヤ + config + client +
テストを実装。既定 `player_auth="off"` で後方互換）

## Date

2026-06-14

## Context

ADR-0025 で「name-pick（L0）→ per-player PIN（L1）→ 外部 IdP（L2）」を additive に進化させる
**方針**を確定した。本 ADR はそのうち **L1（per-player PIN）の詳細設計**を行う。狙いは
LAN 内で「player 本人だけが自分の player_id に対して write（注文作成 / 将来の注文キャンセル等）
できる」ことを軽量に保証することである。

前提・制約（ADR-0025 / ADR-0004 / `docs/contracts/shared-ids.md` から継承）:

- **`player_id` は内部採番・不変**。認証方式が増えても player_id は変わらない。PIN は player_id を
  置き換えず、player に additive に紐づく credential。
- 現行の player read / 注文 POST は **name-pick・無認証・LAN 限定**（ISSUE-0019, 既定 `127.0.0.1`）。
- staff 会計 write の認可は **staff shared token**（ADR-0021）で別途解決済み。L1 はこれと**直交**
  （staff token は店側、PIN は player 本人）。

設計上の論点（ADR-0025 のスケッチを精緻化するために本 ADR で決める点）:

1. PIN credential を **どこに保存するか**（ADR-0025 は「players.json に additive」を素描していた）。
2. **認証フロー**（毎 write で PIN を送るか、検証後にトークンを発行するか）。
3. L0 との **後方互換**（PIN 未登録 player をどう扱うか）。
4. 低エントロピー（4–6 桁）PIN の **総当たり耐性**。

## Decision

### D1. PIN は players.json ではなく **node-local の別ストアに分離**する（ADR-0025 の素描を精緻化）

`players.json` に `pin_hash` を additive する案（ADR-0025）は、以下の理由で採らない:

- `players.json` は **viewer API `/api/players` が serialize して返す** read 対象であり、`pin_hash` を
  Player に持たせると **ハッシュが read API で漏れる**リスクがある（to_dict の取りこぼし事故）。
- `players.json` は **sync の snapshot/merge 対象**（`core/sync.py:build_snapshot`、players/sessions/
  ledger/orders）。credential を載せると**ハッシュが peer に伝播**する。credential は共有ドメインデータ
  ではなく **node-local の秘密**であるべき。
- player schema は `1.0` frozen。別ストアにすれば **schema を一切触らない**（最も安全）。

→ **`player_credentials.json`**（プロジェクト直下、`.gitignore`、アトミックリネーム書き込み）を新設し、
`core/player_credential_repository.py`（`PlayerCredentialRepository`）が source of truth とする。形式:

```json
{"credentials": [
  {"player_id": "ab12...", "pin_hash": "pbkdf2_sha256$210000$<salt_hex>$<hash_hex>",
   "updated_at": "2026-06-14T10:00:00", "failed_attempts": 0, "locked_until": null}
]}
```

- このファイルは **viewer API の read response に含めない**し、**sync snapshot にも含めない**
  （build_snapshot の 4 パスは players/sessions/ledger/orders のまま不変）。
- player 削除時の credential 連動削除は player merge/削除が scope 外のため将来課題（孤児 credential は
  害がない）。

### D2. PIN ハッシュは stdlib の **PBKDF2-HMAC-SHA256 + per-PIN ランダム salt**

- `hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, iterations)`（iterations は config 既定 210000、
  salt は `secrets.token_bytes(16)`）。保存形は `pbkdf2_sha256$<iter>$<salt_hex>$<hash_hex>`。
- 平文 PIN は保存しない・ログに出さない。検証は `hmac.compare_digest` で定時間比較。
- 外部依存を増やさない（argon2/bcrypt は入れない）。4–6 桁 PIN の防御の主役は **LAN 限定 + lockout**
  であり、ハッシュは保存時漏洩への二次防御という位置づけ（下記 D5 の脅威モデル参照）。

### D3. **player principal 解決レイヤ**を API 境界に導入（L1/L2 共通の合流点）

ADR-0025 §3 の「player principal 解決レイヤ」を具体化する。API 境界に

```
resolve_player_principal(request) -> str | None   # 認証済みなら player_id、未認証なら None
```

を置く。player の self-write エンドポイントは「**principal == path の player_id**」を要求する。

**トークン方式（毎回 PIN を送らない）**: PIN 検証成功後、サーバが **stateless な署名トークン**を発行する。

- 形式: `v1.<player_id>.<exp_unix>.<hmac_sha256_hex>`（HMAC は `viewer_api.player_token_secret` で署名）。
- `player_token_secret` は config。未設定なら起動時に `secrets.token_hex(32)` を ephemeral 生成
  （= 再起動でトークン失効、LAN 運用では許容。staff_token と同じく運用で固定したければ config に置く）。
- TTL は config `viewer_api.player_token_ttl_sec`（既定 43200 = 12h）。**サーバ側セッションストアを持たない**
  （HMAC 検証のみ）。L2（ADR-0028）も**同じトークンを発行**するため、principal レイヤは認証方式に依存しない。
- client は write 時に `Authorization: Bearer <player_token>` で送る（staff token とは別ヘッダ値・別 secret。
  サーバは「staff token と一致 → staff」「player token として検証成功 → その player_id」を区別）。

### D4. **opt-in / 後方互換**：config フラグ `viewer_api.player_auth`

| 値 | 意味 |
|----|------|
| `"off"`（**既定**） | 現状維持。L0 name-pick。PIN を一切要求しない（完全後方互換） |
| `"optional"` | **PIN 登録済みの player の write のみ** principal を要求。未登録 player は従来どおり name-pick で write 可 |
| `"required"` | すべての player write に有効な player token を要求（未登録 player は write 不可 = 要 enrollment） |

- **read は既定で name-pick のまま**（小規模クラブの信頼前提）。`required` 時に read も保護したい場合は
  将来 additive に拡張（本 ADR では read は対象外、write の本人性を最優先）。
- 既定 `off` なので**この設計を入れても挙動は変わらない**（ISSUE-0019 の name-pick 出荷と矛盾しない）。

### D5. **総当たり耐性**：per-player lockout（PIN は低エントロピー前提）

- 4–6 桁 PIN は総当たり可能。第一防御は **LAN 限定**（`bind_host` 既定 127.0.0.1、公開は明示 opt-in）。
- 二次防御として `player_credentials.json` に `failed_attempts` / `locked_until` を持ち、**N 回連続失敗で
  クールダウン**（config `viewer_api.pin_max_attempts` 既定 5 / `pin_lockout_sec` 既定 300）。成功で reset。
- PIN は **最低 4 桁**（config `pin_min_length`）。これらは「会場内の善意の利用者の取り違え防止」レベルで、
  敵対的攻撃者に対する強度は LAN 境界に依存する旨を ADR に明記する（過信しない）。

### D6. エンドポイント（すべて additive。既存契約は不変）

| メソッド/パス | 認可 | 内容 |
|---|---|---|
| `POST /api/auth/login` | なし（PIN 自体が credential） | body `{player_id, pin}` → 検証 → `{token, expires_at}`。失敗は 401 `invalid_pin` / lockout 中は 429 `pin_locked` |
| `POST /api/players/{id}/pin` | 初回設定: staff token **または** `player_auth!="off"` での self-enroll（config `pin_self_enroll`）。変更: 現 PIN **または** staff token | body `{pin, current_pin?}` → 登録/変更。staff は reset 可 |
| 既存 player write（注文 POST 等） | `player_auth` の値に従い principal 要求（D4） | `Authorization: Bearer <player_token>` を解決して player_id 一致を確認 |

- error code は `docs/contracts/error-shapes.md` に **auth セクションを additive 追加**して 1:1 対応
  （`invalid_pin` 401 / `pin_locked` 429 / `pin_too_short` 400 / `unauthorized` 401 = principal 不一致 /
  `player_auth_disabled` は該当時 403）。実装時に確定。

## Alternatives Considered

- **players.json に `pin_hash` を additive**（ADR-0025 素描）→ read API 漏れ・sync 伝播・schema 変更の
  3 リスク。→ node-local 別ストアに分離（D1）。
- **毎 write で PIN を送る（トークンなし）** → 実装は単純だが PIN が頻繁に往復しログ/プロキシ露出が増える。
  → 検証 1 回 + 短命署名トークン（D3）。
- **サーバ側セッションストア（stateful token）** → 単一/複数プロセス・再起動・sync で状態管理が増える。
  → stateless HMAC（staff token と同じ軽量路線）。
- **argon2/bcrypt 導入** → 依存追加。LAN + lockout 前提では PBKDF2(stdlib) で十分。→ stdlib。

## Consequences

- Positive: name-pick（L0）から **additive** に本人 write を強化できる（既定 off で挙動不変）。
  principal レイヤを置くことで **L2（外部 IdP）が同じトークン契約に load** でき、エンドポイントが
  認証方式に非依存になる。player schema（1.0 frozen）も sync も触らない。
- Negative / trade-offs: 4–6 桁 PIN の強度は LAN 境界 + lockout に依存（敵対環境では弱い）。
  credential が node-local なので **複数ノードへ PIN は自動同期されない**（L1 は単一会場 LAN 前提で十分。
  マルチノード会員認証は L2 hosted モードの担当）。新ストア 1 個と auth 3 エンドポイントが増える。
- Neutral: config に `viewer_api.player_auth` ほか数フラグ追加。`error-shapes.md` に auth セクション追加。

## Validation / Follow-up

- [x] `core/player_credential_repository.py`（PBKDF2 + lockout + アトミック永続）。
- [x] principal 解決ユーティリティ（`core/auth_token.py` の HMAC トークン発行/検証）+ `api/server.py` の
  `_resolve_player_principal` / `_require_player` + 注文 POST 結線 + `/api/auth/login` / `/api/players/{id}/pin`。
- [x] config 既定（`player_auth="off"`）で全既存テスト不変を確認（557 passed、後方互換）。
- [x] auth エンドポイント + lockout + principal 不一致の単体/結合テスト
  （`tests/test_auth_token.py` / `tests/test_player_credential_repository.py` / `tests/test_viewer_api_auth.py`）。
- [x] `error-shapes.md` auth セクション + `viewer-api.md` の auth endpoints。
- [ ] （follow-up）read の本人保護（現状 read は対象外）、player 削除時の credential 連動削除、
  GUI からの PIN 設定 UI。

## Related Files

- 新規: `core/auth_token.py`, `core/player_credential_repository.py`,
  `player_credentials.json`（.gitignore）, `tests/test_auth_token.py`,
  `tests/test_player_credential_repository.py`, `tests/test_viewer_api_auth.py`
- 変更: `api/server.py`（principal 解決 + login/pin + 注文 POST gate + `auth_kwargs_from_config`）,
  `api/client.py`（`login` / `set_pin` / player token）, `config_default.json`, `main.py`（結線）,
  `docs/contracts/error-shapes.md` / `docs/contracts/viewer-api.md`
- 不変: `core/player.py` / `players.json` / player schema（`1.0` frozen）/ `core/sync.py`（snapshot 4 パス）

## Related Tests

- `tests/test_auth_token.py` / `tests/test_player_credential_repository.py` / `tests/test_viewer_api_auth.py`

## Related Commits

- 本 ADR の実装 commit（2026-06-14）

## Supersedes / Superseded by

- Supersedes: —（ADR-0025 L1 を詳細化。ADR-0025 の「players.json に pin_hash」素描を node-local 別ストアに
  **精緻化**。関連: ADR-0025 / ADR-0021（staff token と直交）/ ADR-0004（player_id 不変）/ ISSUE-0019）
- Superseded by: —
