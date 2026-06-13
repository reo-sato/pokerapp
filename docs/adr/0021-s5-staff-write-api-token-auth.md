# ADR-0021: S5 write expansion — staff accounting write API gated by a staff shared token

## Status

Accepted

## Date

2026-06-13

## Context

ADR-0020 で S5 の **read boundary** を二言語（mobile TS / Python `ViewerApiClient`）で確立したが、
write は player の注文 POST（pending only, name-pick, 無認証, ADR-0018）だけが HTTP に出ていた。
会計の実 write（ledger 追加 / settlement 確定 / paid-unpaid / 注文確定・却下）は `--ledger` プロセスの
GUI スレッドからしか行えず、**別端末のスタッフが会計をリモート操作できない**。

これを HTTP に出すには 2 つの blocker がある:

1. **認証**: player read / 注文 POST は ISSUE-0019 で「v1 = name-pick / 無認証」と決めたが、会計 write は
   金銭を動かすため無認証では出せない。per-player PIN（ISSUE-0019 の将来案）はまだ未確定。
2. **並行性**: `LedgerRepository` はロックを持たない（thread-safe なのは `OrderRequestRepository` だけ）。
   会計 write を HTTP に出すと、`--ledger` プロセスで **viewer API スレッドと GUI スレッドが同じ
   `LedgerRepository` を mutate** し、実データレースになる。

## Decision

S5 write 拡張の **第一歩 = スタッフ会計 write を staff shared token で HTTP に出す**。

1. **認証 = staff shared token**（最小・運用が単純）。config `viewer_api.staff_token` を設定すると、
   `/api/staff/...` のスタッフ会計エンドポイントが `Authorization: Bearer <token>` で有効になる。
   token が falsy（空 / 未設定）なら staff write は **403 `staff_writes_disabled`**（無効）、token 不一致は
   **401 `unauthorized`**。**player read / 注文 POST は従来どおり name-pick / 無認証**（変更なし）。
2. **write scope = スタッフ会計 write**（別端末でのリモート会計運用を可能にする最小集合）:
   (a) ledger entry 追加、(b) settlement 確定（commit）、(c) payment status paid/unpaid、
   (d) order 確定、(e) order 却下。これを駆動する **staff read** も足す:
   (f) session の settlement 中間集計、(g) session の order request 一覧（全 player の queue）。
3. **単一書き手は維持**（ADR-0020）。staff *write*（need_write）は **write を所有するプロセス
   （`--ledger`, orders_writable=True）でのみ**有効。単独 `--viewer-api`（read-only）では staff write は
   **503 `orders_unavailable`**（staff *read* は token があれば可）。これで「別プロセスからの lost update」
   を構成で排除し続ける。
4. **`LedgerRepository` を thread-safe にする**（`OrderRequestRepository` に倣う）。`threading.RLock` を
   `__init__` に持たせ、読み書き公開メソッド（add_entry / reverse_entry / grant_points / point_balance /
   list_point_entries / list_entries / compute_settlement / commit_settlement / list_settlements /
   all_settlements / set_payment_status）を module-level `_locked` デコレータで囲む。**業務ルールは不変**
   （ロックを足すだけ）。RLock は再入可能なので、ロック済みメソッドが他のロック済みメソッドを呼んでも
   デッドロックしない（例: `add_entry` → `point_balance`）。
   - **lock ordering**: order-confirm は常に order-lock → ledger-lock の順で取る
     （`OrderRequestRepository.confirm_request` → `ledger.add_entry`）。新しい staff エンドポイントは
     order_repo か ledger_repo の**どちらか一方**しか触らない（ledger→order の逆順取得は発生しない）ため、
     2 ロック間のデッドロックは構造的に起きない。
5. **player PIN は引き続き先送り**（ISSUE-0019）。staff write の認可は shared token で解決済み。
   per-player の read アクセス制御が必要になったら additive に PIN を入れる（別 ADR）。

エンドポイント（すべて staff token 必須。player API と分けるため `/api/staff/` 配下）:

| method | path | need_write | 説明 |
|--------|------|-----------|------|
| GET  | `/api/staff/sessions/{sid}/settlement` | no | settlement 中間集計（compute_settlement） |
| GET  | `/api/staff/sessions/{sid}/order-requests?status=` | no | session の注文 queue（全 player） |
| POST | `/api/staff/sessions/{sid}/ledger-entries` | yes | ledger entry 追加（201） |
| POST | `/api/staff/sessions/{sid}/settlement/commit` | yes | settlement 確定 |
| PUT  | `/api/staff/sessions/{sid}/players/{pid}/payment-status` | yes | paid/unpaid |
| POST | `/api/staff/order-requests/{rid}/confirm` | yes | 注文確定（ledger order entry を起こす） |
| POST | `/api/staff/order-requests/{rid}/reject` | yes | 注文却下 |

error code は error-shapes.md の ledger / order セクションを **再利用**（not_found / unknown_player /
invalid_amount / entry_fee_requires_cash / insufficient_points / session_not_closed / already_settled /
session_closed / already_resolved）+ 新規 `unauthorized`(401) / `staff_writes_disabled`(403)。

## Alternatives Considered

- **per-player PIN を今入れる** — ISSUE-0019 で「v1 は name-pick、PIN は将来再評価」と決めたばかりで、
  PIN の発行・配布・回転・忘却の運用が未確定。会計 write はスタッフだけが行うので、player ごとの
  認証ではなく **単一の staff token** が適切（誰が会計をするかは店側の信頼境界）。→ shared token。
- **無認証のまま会計 write を出す** — 金銭を動かす write を LAN 上で無認証公開するのは不可。read /
  注文 POST（pending）と決定的に異なる。→ token 必須。
- **OAuth / per-staff アカウント / セッション cookie** — 単一店舗・少人数運用に対し過剰。token 1 本で
  「会計端末を信頼する」モデルが運用に合致。必要が出たら additive に拡張。→ shared token。
- **`LedgerRepository` を per-method lock や粗粒度の単一グローバルロックにする** — RLock + デコレータが
  `OrderRequestRepository` と対称で最小差分。業務ロジックに触れずレースだけ消せる。→ RLock。

## Consequences

- Positive: 別端末のスタッフが会計（ledger / settlement / 注文確定）をリモート操作できる。`LedgerRepository`
  が thread-safe になり、in-process API と GUI の同時 mutate が安全。read boundary（ADR-0020）に
  write boundary が揃い、S5 write 拡張の第一歩が完了。
- Negative / trade-offs: 認証が shared token（per-staff 監査・失効はできない。token 漏洩 = 会計 write 漏洩）。
  LAN 限定前提は維持。standalone `--viewer-api` では staff write が 503 になる（単一書き手の制約）。
- Neutral: `config_default.json` に `viewer_api.staff_token`（既定空 = 無効）を追加。`ViewerApiClient` に
  `staff_token` + staff メソッド群を additive 追加。

## Validation / Follow-up

- [x] `api/server.py` staff endpoints + `_staff_guard`（403/401/503）+ ledger error map。
- [x] `LedgerRepository` に RLock + `_locked` デコレータ（業務ロジック不変、既存 test 緑）。
- [x] `api/client.py` に `staff_token` + staff メソッド。`tests/test_viewer_api_staff.py`（happy path + 認可失敗 + error code）。
- [ ] 双方向 sync / 衝突解決（複数書き手）は別 ADR（単一書き手で当面足りる）。
- [ ] player read の per-player アクセス制御（PIN, ISSUE-0019）は問題が顕在化したら additive。

## Related Files

- `api/server.py` / `api/client.py` / `main.py`（`run_ledger_view` / `run_server` の staff_token 結線）
- `core/ledger_repository.py`（RLock + `_locked`）
- `config_default.json`（`viewer_api.staff_token`）
- `docs/contracts/viewer-api.md` / `error-shapes.md` / `repository-interfaces.md`

## Related Tests

- `tests/test_viewer_api_staff.py`（staff write round-trip / 認可 / error code）
- `tests/test_ledger_repository.py`（lock 追加後も緑） / `tests/test_viewer_api.py` / `tests/test_viewer_api_client.py`

## Related Commits

- 本 ADR と同じ commit（S5 staff write API）

## Supersedes / Superseded by

- Supersedes: —（関連: ADR-0017（viewer API）/ ADR-0018（orders, single-writer）/ ADR-0020（read boundary）。ISSUE-0019（privacy model）の staff write 認可を解決）
- Superseded by: —
