# ADR-0030: player merge の設計（alias / tombstone 方式・read-time canonicalization）

## Status

Accepted（**実装済 2026-06-14**。alias/tombstone + read-time canonicalization を core / API / GUI /
sync に実装。player schema `1.0`→`1.1` を適用。既存挙動は不変＝580 passed）

## Date

2026-06-14

## Context

同一人物が **複数の `player_id`** を持ってしまうケースを統合（merge）する手段が無い。発生源:

- **L2 の前提（ADR-0029 D1）**: cloud で LINE/Google サインアップした player（新規 player_id, ADR-0028 D4）と、
  会場で name-pick 用に作られた既存 player は **別 player_id** になる。両者を結びつける merge が L2 の
  ブロッカー。
- **一般の重複**: スタッフが「Alice」を二重作成した等、LAN 単独でも起こる。

`player_id` は **アプリ内採番・不変・全データの安定キー**（ADR-0004）で、以下から参照される:

| 参照元 | キーの使われ方 | 同期 |
|--------|----------------|------|
| `players.json`（registry） | player 本体 | 双方向 sync |
| `sessions.json`（seat_assignment） | hand ごと seat→player スナップショット | 双方向 sync |
| `ledger.json`（entry / point / settlement） | 金銭イベント・集計の帰属 | 双方向 sync（**append-only**, ADR-0016） |
| `order_requests.json` | 注文の帰属 | 双方向 sync |
| `player_credentials.json`（L1 PIN） | credential のキー | **node-local（sync 非対象）** |
| `auth_identity`（L2） | `(provider,sub)→player_id` | **node-local（sync 非対象）** |
| hand logs `logs/{sid}.json` | `players[].player_id`（best-effort, 帰属判定に非使用） | file 単位 |

制約となる既存不変条件:

- **ledger は append-only**（entry を mutate/delete しない。訂正は reversal, ADR-0016）。
- **sync は state-based の収束マージ**（可換・結合・冪等、UUID union + 単調解決, ADR-0022）。
- **player_id は不変・内部採番**（外部 sub から導出しない, ADR-0004）。

## Decision

### D1. merge = **alias / tombstone**（履歴を書き換えない）

被吸収 player（absorbed, B）の registry レコードに **`merged_into: <survivor_id>`**（+ `merged_at`）を
付ける。**既存の ledger / session / order / hand-log レコードの `player_id` は一切書き換えない**。

なぜ rewrite ではなく alias か:

- **append-only と整合**: ledger entry を mutate しない（rewrite は append-only 違反）。
- **収束 sync と整合**: `merged_into` は player レコードへの **monotonic な additive 情報**。一度 merge
  されたら戻らない単調値として伝播でき、ADR-0022 の収束マージに乗る（多数レコードの player_id を
  rewrite する非単調・大規模変更は収束しない）。
- **可逆**: 破壊していないので unmerge が `merged_into` 除去だけで済む（D4）。

### D2. read-time canonicalization（読み取り時に survivor へ解決）

`PlayerRepository.resolve_canonical(player_id) -> str` を追加し、`merged_into` チェーンを survivor まで
辿る（**サイクル検出 / 深さ上限**つき）。**player_id をキーにする全 read を canonicalize** する。具体箇所:

- **settlement 集計**（`core/ledger_repository.py:compute_settlement`。`player_id` で group: 行 487–496）→
  canonical でグルーピングすると、同一人物の複数 player_id 分が 1 行に合算される。
- **viewer の per-player read**（`api/read_models.py`: `list_player_sessions` / `list_player_hands` の
  seat_assignment 値突合、`get_player_session_ledger` の settlement/entry フィルタ）。
- **seating 導出**（`core/session_repository.py:current_seating` / `resolve_seat_map_for_hand` の表示解決）。
- **order フィルタ**（`core/order_request_repository.py:list_requests` の `player_id==`）。
- **login principal**（L1: `verify_pin` 後 / L2: auth_identity 解決後の player_id を canonical に正規化）→
  merge 後は LINE/Google でログインしても survivor の principal になる（auth_identity の rewrite 不要）。
- **registry の list/get**: tombstone（`merged_into!=null`）は一覧から隠す or「→ survivor に統合済」表示。

業務ルールは **core が source of truth**（ADR コーディング規約）。canonicalize は registry の
`resolve_canonical` を各 read 境界が呼ぶ形に集約し、front-end には複製しない。

### D3. survivor 選択 = スタッフが明示（既定提案あり）

- merge はスタッフ操作。survivor / absorbed をスタッフが明示選択する。
- **既定提案**: 会計履歴を持つ / `created_at` が古い方を survivor（通常は会場 player）、cloud サインアップ
  player を absorbed にする。L2 では「cloud の新規 player を会場 player に吸収」が典型。
- survivor の `display_name` を残す。absorbed の旧名は検索性のため alias note として保持してよい（任意）。

### D4. 可逆（unmerge）

破壊しないため、`merged_into` を除去すれば unmerge できる（誤統合のリカバリ）。L1 credential を absorbed から
落としていた場合は再設定が要る点のみ注記。

### D5. sync の収束（conflict 解決）

- `merged_into` は player レコードの additive monotonic field。通常は一方向にのみ増える。
- **conflict**（2 ノードが A をそれぞれ B / C に merge）: 決定的 tiebreak で全ノード収束させる
  — survivor を **安定キー最小**（または `merged_at` 最早→stable key）で選ぶ、を ADR-0022 の player 解決に
  additive 追加。チェーン（A→B→C）は `resolve_canonical` が終端まで辿る。
- credential / auth_identity は node-local（sync 非対象）なのでここでは衝突しない。

### D6. エッジケース

- **同一 hand 内の重複**: 過去の seat スナップショットは書き換えない（D1）。A が seat1・B が seat2 の hand を
  後で merge すると、`current_seating` 表示上は canonical player が 2 席に見えうる = **歴史的アーティファクト**
  として扱う（自動解決しない）。settlement 集計は両者を合算 = 同一人物の総額として正しい。
- **チェーン / 二重 merge**: survivor が後で別へ merge された場合、`resolve_canonical` がチェーン終端まで
  解決。target が既に tombstone なら、その canonical を survivor に採る。
- **深さ上限 / サイクル**: 解決は上限 N 段 + visited セットでガードし、異常時はログ + 元 id を返す。

### D7. スコープ / 所有

- **staff 専用操作**（player-facing ではない）。desktop GUI（registry 画面に merge）+ staff API
  `POST /api/staff/players/merge`（body `{survivor_id, absorbed_id}`, staff token gate, ADR-0021）。
- absorbed の L1 credential は削除、survivor の credential を残す。auth_identity は rewrite 不要
  （canonical 解決でログインが survivor に向く。任意で repoint 最適化）。
- **registry の一般機能**として実装し、LAN 単独の重複掃除にも使える（L2 専用ではない）。

## Alternatives Considered

- **player_id を全レコードで rewrite（survivor に置換）** → ledger append-only 違反・不可逆・**収束 sync で
  破綻**（多数レコードの非単調 rewrite は 2 ノードで分岐し収束しない）。→ 不採用（D1）。
- **absorbed を物理削除** → ledger / hand 履歴が dangling player_id を参照し read が壊れる。→ 不採用。
- **merge を作らず手動 dedup** → L2 のブロッカーが残る・会計が分裂表示。→ 不採用。
- **auth_identity を survivor に rewrite して registry は触らない** → registry の重複が残り会計が分裂。
  merge の本体は registry の alias であるべき。→ alias + （任意で）auth_identity repoint。

## Consequences

- Positive: L2（cloud signup ↔ 会場 identity）の前提が解ける。**append-only・収束 sync・player_id 不変を
  保ったまま** 同一人物を 1 会計に統合。**可逆**。LAN 単独の重複掃除にも使える。
- Negative / trade-offs: **player_id をキーにする全 read が canonicalize を呼ぶ規律**が要る（取りこぼすと
  分裂表示）。→ `resolve_canonical` に集約 + 各 read 境界の canonicalize を固定するテストで担保。
  sync に conflict tiebreak を additive 追加。
- Neutral: `player` schema に optional `merged_into` / `merged_at` を additive 追加（**`1.0`→`1.1`**、
  settlement の partial-paid `1.1` と同じ frozen 規則内の additive, ADR-0019/0023 先例）。

## Validation / Follow-up（実装結果）

- [x] `player` schema additive bump（`1.0`→`1.1`, optional `merged_into`/`merged_at`）+ `valid-merged`
  fixture + code↔contract test 緑。`Player.to_dict` は未 merge では従来の形を保つ。
- [x] core: `PlayerRepository.merge_players` / `resolve_canonical`（チェーン/サイクル/深度ガード）/
  `equivalence_class` / `unmerge` / `list_players(include_merged=False)` で tombstone を隠す。
- [x] 各 read 境界の canonicalize: settlement 集計（`_derive_settlement_rows`）/ point 残高 /
  `list_entries` フィルタ / order `list_requests` フィルタ / viewer per-player read（`api/read_models`）/
  login principal + `_require_player`（`api/server.py`）。
- [x] sync の player 解決に merge conflict tiebreak（`core/sync.py:_resolve_player`、monotonic +
  survivor 最小）。
- [x] staff API `POST /api/staff/players/merge` + `ViewerApiClient.merge_players` + registry GUI
  （統合先設定→統合）。
- [x] tests: `tests/test_player_merge.py`（core + cross-repo）/ `tests/test_viewer_api_merge.py`
  （staff + viewer + login-through-merge）/ `tests/test_player_registry_gui.py::TestMergePlayers` /
  `tests/test_sync.py`（monotonic 伝播 + tiebreak 収束）。
- [~] 歴史的 seat スナップショットの「同一 hand 2 席」表示（D6）は read-time の歴史的アーティファクトとして
  許容（自動解決しない）。`current_seating` の表示 canonicalize は未対応（staff 検査画面のみ・低影響, follow-up）。

## Related Files

- `core/player.py` / `core/player_repository.py`（merge + resolve_canonical）
- `core/ledger_repository.py`（settlement 集計の canonicalize）/ `api/read_models.py`（viewer read）/
  `core/session_repository.py`（seating）/ `core/order_request_repository.py`（order フィルタ）
- `core/sync.py`（player conflict tiebreak）/ `docs/contracts/schemas/player.schema.json`（`1.1` additive）
- `api/server.py`（staff merge endpoint）/ `gui/player_registry.py`（merge UI）

## Related Tests

- `tests/test_player_merge.py` / `tests/test_viewer_api_merge.py` /
  `tests/test_player_registry_gui.py::TestMergePlayers` / `tests/test_sync.py`（player merge 節）

## Related Commits

- 本 ADR の設計 + 実装 commit（2026-06-14）

## Supersedes / Superseded by

- Supersedes: —（CLAUDE.md「player 削除 / merge は scope 外」を merge について設計に格上げ。関連:
  ADR-0028/0029（L2 前提）/ ADR-0004（player_id 不変）/ ADR-0016（append-only）/ ADR-0022（収束 sync）/
  ADR-0019（schema freeze の additive 規則））
- Superseded by: —
