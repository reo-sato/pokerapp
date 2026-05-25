# Shared ID contract

`player_id` / `session_id` / `hand_id` は core / desktop / mobile / 将来の API・sync が共有する
**安定キー**。これらは全 workstream の最上流契約であり、最初に凍結する（freeze order #1）。

共通原則:

- **アプリ内採番**: ID の採番はアプリ内で完結する。外部システム（決済代行・会員 DB 等）に
  採番を依存しない。sync 導入後も、各 ID はアプリ内で発行した値が不変キーとして残る。
- **不変（immutable）**: 一度発行した ID は変化しない。display_name などの属性が変わっても ID は同じ。
- **文字列として扱う（cross-app 境界）**: アプリ間・永続層・将来 API では ID を **opaque string** として
  受け渡す。内部表現が int であっても、境界を越える表現は文字列に正規化する（後述 hand_id 参照）。
- **意味を埋め込まない**: ID 文字列から属性を推測しない（display_name や席番号を ID に含めない）。

---

## `player_id`

| 項目 | 契約 |
|------|------|
| 役割 | player を一意に識別する。session / ledger / settlement / seat_assignment から参照される。 |
| 一意性スコープ | アプリ（= 店の registry）内で globally unique。 |
| 文字列形式 | UUID4 の hex 表現（32 文字, lowercase, ハイフンなし）。正規表現 `^[0-9a-f]{32}$`。 |
| 生成責務 | player registry（`core/player_repository.PlayerRepository.create_player`）。**S1 で実装済**。 |
| 永続性 | `players.json` に永続化。アプリ再起動を跨いで安定（S1 のテストで固定済）。 |
| cross-app 参照 | 他 model / front-end は `player_id` 文字列のみで参照する。display_name で参照しない。 |

現状実装: `uuid.uuid4().hex`（`core/player_repository.py`）。本契約と一致。

---

## `session_id`

| 項目 | 契約 |
|------|------|
| 役割 | 1 卓 1 回の運営単位（session）を一意に識別する。ledger_entry / settlement / hand_ref が参照。 |
| 一意性スコープ | アプリ（店）内で globally unique。 |
| 文字列形式 | opaque な非空文字列。**推奨**は UUID4 hex（player_id と同形式）。 |
| 生成責務 | session レイヤ（**planned, S2**）。session 開始時に採番する。 |
| 永続性 | session を跨いで安定。settlement 確定後も不変参照として残る。 |
| cross-app 参照 | ledger app / hand logger は `session_id` 文字列で相互参照する。 |

現状実装（hand logger）: `datetime.now().strftime("%Y-%m-%d_%H%M%S") + "_session1"`（`main.py`）。
これは opaque 文字列契約は満たすが UUID ではない。**S2 で session レイヤを実装する際に
採番方式を確定する**（timestamp 文字列を残すか UUID に統一するか）。本 doc は契約として
「opaque・非空・アプリ内採番・不変」を要求し、具体採番は S2 freeze で確定する。

---

## `hand_id`

| 項目 | 契約 |
|------|------|
| 役割 | hand logger 側の 1 ハンドを識別する。ledger は `hand_ref` を介して参照する。 |
| 一意性スコープ | **現状: session 内で unique**（per-session の連番 int）。グローバル一意ではない。 |
| 文字列形式 | 現状 int。cross-app 境界では **`(session_id, hand_id)` の複合キー** で globally unique にする。 |
| 生成責務 | hand logger（`GameStateManager` のハンド採番）。 |
| 永続性 | session JSON 内で安定。 |
| cross-app 参照 | `hand_ref` が `{session_id, hand_id, started_at, seat_assignments}` を保持し、ledger 側は
                  これを不変参照として読む（**planned, S2**）。 |

> **既知の不整合（要 reconcile）**: 共有 ID 原則は「cross-app では文字列 opaque」だが、現状の
> hand logger は `hand_id: int`（per-session 連番, `core/hand_log.py`）。グローバル一意性は
> `(session_id, hand_id)` 複合でしか満たせない。これを S2 の `hand_ref` 設計で
> 「複合キーのまま contract 化するか / 文字列 hand_id に正規化するか」確定する。
> 詳細・選択肢は `docs/issues/0006-hand-id-int-vs-cross-app-string.md` を参照。

---

## まとめ（freeze 状態）

| ID | 形式 | 採番責務 | 実装状況 | freeze 状態 |
|----|------|---------|---------|------------|
| `player_id` | UUID4 hex (32) | player registry | ✅ S1 実装済 | freeze 候補（S1 で安定） |
| `session_id` | opaque string（UUID 推奨） | session layer | 🔲 planned (S2) | 未 freeze（S2 で確定） |
| `hand_id` | int（session 内）/ 複合キー | hand logger | ✅ 実装済（int） | 未 freeze（S2 で cross-app 形を確定） |

凍結手順は `versioning-and-freeze.md` を参照。
