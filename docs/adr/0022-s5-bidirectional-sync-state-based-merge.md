# ADR-0022: S5 — 双方向 sync（state-based merge / append-only union + 単調フィールド解決）

## Status

Accepted

## Date

2026-06-13

## Context

ADR-0020 は cross-app boundary の **read** を確立し、同期方式を **on-demand pull + 単一書き手**とし、
**双方向 auto-sync は「衝突解決方針が前提」として明示的に先送り**した。今回これを実装する。

想定トポロジ（v1）: **小規模クラブの LAN 上に複数の運営ノード**（例: ハンドロガー卓の PC が
session/seating/hand を、キャッシャー卓の PC が ledger/settlement を主に書く）。各ノードは全ストアの
レプリカを持ち、相互に最新化したい。中央サーバや常時接続は前提にしない。

このアプリのデータ形が sync を tractable にする鍵:

- **大半が append-only + app 内採番 UUID キー**: `ledger_entry`/`point_ledger_entry`（entry_id）、
  `order_request`（request_id）、`seat_assignment`（(session_id,hand_id,seat_no)）、`player`（player_id）。
  同一 UUID は同一内容を意味し、異なる UUID は両立する ⇒ **union で衝突なく統合**できる。
- **可変フィールドは単調 progression**: `order_request.status`（pending→confirmed|rejected の終端）、
  `session.status`（open→closed）、`settlement`（uncommitted→committed、unpaid→paid）。
  ⇒ **「進んだ方を採用」する決定的ルール**で収束する。

これは state-based（CvRDT 的）マージであり、UUID union + 単調解決により merge は
**可換・結合的・冪等**になる ⇒ どのノードがどの順で何度マージしても同じ状態に収束する。
衝突解決アルゴリズム（vector clock 等）を v1 では不要にできる。

## Decision

1. **peer-to-peer の on-demand state-based merge**を採用する（中央権威なし・常時 auto-sync なし・
   push/event なし）。sync = 各ノードが相手の **snapshot（全レコード）** を取得して **merge** する。
   双方向 = A が B の snapshot を merge し、B が A の snapshot を merge する（収束）。
   ADR-0020 の「単一書き手」は **複数書き手 + 収束マージ**に置き換わる（本 ADR が更新）。
2. **マージは純粋関数**（`core/sync.py`、dict レコードのリストに対して動作）。各ストアのルール:
   - **players**（key `player_id`）: **create-only union**。新規 id は追加、既存 id は **local 優先**
     （display_name の rename は v1 では伝播しない＝`updated_at` 不在のため。会計に影響しないため許容、
     伝播は将来 `updated_at` additive で対応）。
   - **ledger_entries / point_ledger_entries**（key `entry_id`）: **union**（衝突なし。同一 id ⇒ 同一）。
   - **order_requests**（key `request_id`）: union + status 解決。pending < {confirmed, rejected}。
     一方が終端・他方 pending → 終端を採用（resolved_at / ledger_entry_id ごと）。両終端で status が
     異なる稀な衝突（confirmed vs rejected）→ **confirmed 優先**（会計影響があり ledger entry が存在する）。
     両 confirmed は resolved_at の早い方。
   - **settlements**（key `(session_id, player_id)`）: committed（settled_at 非 null）が uncommitted に
     勝つ。両 committed なら **paid が unpaid に勝つ**（支払いは取り消されない単調性）、settled_at は早い方、
     金額等は committed 側の値を保持。
   - **sessions**（key `session_id`）: union。`status` は **closed が open に勝つ**、`ended_at` は closed 側。
     `label`/`blinds` は local 優先（peer のみにあれば採用）。入れ子 `hands`（key hand_id）は union、
     各 hand の `seats`（key seat_no）も union（同一 (hand,seat) が別 player の稀な衝突は **local 優先**で
     決定的に）。
   - **hand log（`logs/*.json`）は v1 sync 対象外**（大きい append-only ファイル。将来 file-level union）。
3. **マージ手順**: `core/sync.py` がローカルの各ストア JSON を読み、peer snapshot を上記ルールで merge し、
   アトミックに書き戻す。live プロセスは各 repo を `reload()` して in-memory を最新化
   （`LedgerRepository` / `OrderRequestRepository` に `reload()` を additive 追加）。
4. **公開とアクセス制御**: sync は **staff-only**（`Authorization: Bearer <viewer_api.staff_token>`,
   ADR-0021 と同じ）。`GET /api/staff/sync/snapshot`（自ノードの全レコード）/
   `POST /api/staff/sync/merge`（peer snapshot を取り込み）。LAN 限定前提を維持。write 所有
   （`orders_writable`）プロセスでのみ merge 受理（read-only は 503）。
5. **ID 不変性が前提**: player_id/session_id/entry_id/request_id は app 内採番 UUID で不変・グローバル一意
   （ADR-0004/0007）。これが UUID union の正しさを保証する。

## Alternatives Considered

- **vector clock / OT / 一般 CRDT で任意フィールドの並行編集を解決** — append-only + 単調という本データの
  性質に対し過剰。実装・検証コストが S5 の規模を超える。→ append-only union + 単調解決に限定。
- **常時 auto-sync（push/event/watch）** — 部分障害・順序・再送の設計が必要。小規模 LAN では on-demand
  pull で足り、運用が単純。→ on-demand（手動/定期トリガ）に限定。
- **中央サーバ権威（single source）** — 単一障害点を作り、ADR-0004 の「app 内採番・外部前提なし」に反する。
  → peer-to-peer。
- **player rename を伝播させる** — `updated_at` 版数が要る（frozen schema への additive 追加）。会計に
  影響せず v1 の必須要件でないため create-only union に留め、将来 additive で対応。

## Consequences

- Positive: 複数運営ノードが LAN 上で収束。merge が可換・冪等なので運用が単純（何度同期しても安全）。
  会計コア（append-only）は衝突ゼロで統合。ADR-0020 の boundary を双方向に拡張。
- Negative / trade-offs: player rename と (hand,seat) の稀な並行衝突は local 優先で決定的だが「伝播しない/
  一方が勝つ」semantics（document 済）。settlement の post-commit 再計算差は従来同様の caveat。
  常時自動同期ではなく手動/定期トリガ。
- Neutral: ADR-0020 の「単一書き手」前提を本 ADR が更新（複数書き手 + 収束マージ）。sync は staff-token gate。

## Validation / Follow-up

- [x] `core/sync.py` 純粋マージ関数 + 収束性テスト（可換 `merge(A,B)≅merge(B,A)` / 冪等
  `merge(A,A)=A`, `merge(merge(A,B),B)=merge(A,B)` / 単調解決）。
- [x] snapshot/merge endpoints（staff-gate）+ `ViewerApiClient.sync_*` + 2 ノード収束 round-trip test。
- [ ] player rename 伝播（`updated_at` additive）/ hand log の file-level union / 定期自動トリガは後続。

## Related Files

- `core/sync.py`（新規）/ `core/{ledger,order_request}_repository.py`（`reload()` additive）
- `api/server.py`（sync endpoints）/ `api/client.py`（sync methods）
- `docs/contracts/{viewer-api,repository-interfaces}.md`

## Related Tests

- `tests/test_sync.py`（純粋マージ収束）/ `tests/test_viewer_api_sync.py`（2 ノード round-trip）

## Related Commits

- 本 ADR と同じ commit（双方向 sync）

## Supersedes / Superseded by

- Supersedes: —（ADR-0020 の「単一書き手 / 双方向 auto-sync 先送り」を更新。関連: ADR-0004/0017/0021）
- Superseded by: —
