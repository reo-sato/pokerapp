# ADR-0020: S5 — cross-app boundary（repository interface 契約凍結 + Python API client 分離）

## Status

Accepted

## Date

2026-06-13

## Context

ロードマップ S5 は「同一プロセス前提から、別プロセス / 別アプリ + API/sync へ移行できる boundary を
切り出す」フェーズ。done criteria は「front-end が repository interface のみに依存したまま
local↔API backend を切り替えられる」「ID が backend を跨いで安定」。Blockers として「参照同期方式の
ADR」「ID 不変性の保証」「衝突解決方針」が挙がっていた（CLAUDE.md § Phase 5）。

統合後の現状で S5 の一部は **既に実現済み**:

- **read-only HTTP boundary**: M1 viewer API（`api/`, ADR-0017）が player_id/session_id/hand_id 越しに
  hand/ledger を read で公開。
- **front-end の backend 差し替え実証**: mobile（M2）は `ViewerRepository` interface に
  `MockRepository`（fixtures）/ `HttpRepository`（API）を `EXPO_PUBLIC_API_URL` で注入切替済み
  ＝ ADR-0004 の「UI 無改修で local↔API を切替」境界を TypeScript 側で実証。
- **ID 安定性**: player_id/session_id は app 内採番の UUID4 hex で不変（ADR-0004/0007）。backend を
  跨いでも変わらない。
- **write の単一所有**: 注文 write は staff-confirm（ADR-0018）+ 単一プロセス所有で、別プロセス間の
  lost update を構成で排除済み。

残るのは (a) **repository / service interface 契約の凍結**（freeze order #6）、(b) **Python 側にも
local↔API client の分離点を用意**（mobile の HttpRepository に相当する Python 実装）、(c) write/sync の
拡張範囲の決定。

## Decision

S5 を **read boundary の確立まで**を本 ADR のスコープとし、双方向 auto-sync は将来課題として分離する。

1. **repository / service interface 契約を `frozen` にする**（freeze order #6）。
   `docs/contracts/repository-interfaces.md` が player / session-seating / ledger-points-settlement /
   viewer read model / order-request の interface を網羅しており、対応 schema は ADR-0019 で `1.0`。
   interface はこれをもって安定契約とする（追加メソッドは additive、シグネチャ変更は ADR）。
2. **同期方式 = on-demand pull**（v1）。push / event / 双方向 auto-sync は持たない。read は HTTP GET の
   都度取得、write は所有プロセスが行う（注文 = staff-confirm, ADR-0018）。**衝突解決は「単一書き手 +
   reload-on-read」で回避**（OrderRequestRepository が既に採用）。これにより衝突解決アルゴリズムを
   v1 では不要にする。
3. **Python API client を分離点として追加**（`api/client.py:ViewerApiClient`）。mobile の
   `HttpRepository`（TS）に相当する Python 実装で、viewer API の read endpoints（+ 注文 GET/POST）を
   呼び、非 2xx を error-shape の `code` を載せた `ViewerApiError` に変換する。これで「Python 側の
   front-end / 別プロセスも同じ HTTP 契約で読める」ことを実証し、**API ↔ client の round-trip 契約
   test**（`tests/test_viewer_api_client.py`）で境界の drift を検知する（versioning-and-freeze.md
   §将来拡張の「consumer-driven contract / round-trip」を最小実装）。
4. **ID 不変性を契約として明記**: `shared-ids.md` のとおり player_id/session_id は app 内採番・不変・
   backend 非依存。API client / local 実装のいずれでも同じ ID を返す。

## Alternatives Considered

- **双方向 auto-sync（push / event）を今入れる** — 衝突解決・部分障害・順序保証の設計（S5 並の重さ）が
  必要で、現状の単一店舗・単一書き手の運用に対し過剰。→ v1 は on-demand pull + 単一書き手で回避し、
  必要が出たら別 ADR。
- **interface 契約を freeze しない** — front-end（mobile / 将来の Python client）が「いつ変わるか
  分からない」抽象に依存し続ける。schema は 1.0 なのに interface だけ draft は不整合。→ freeze。
- **Python client を作らず mobile の HttpRepository だけで boundary 実証済とする** — TS 側だけでは
  「Python の別プロセス / 別 front-end が同契約で読める」ことと round-trip drift 検知が示せない。
  最小の Python client + 契約 test で境界を二言語で固定する。→ 追加。
- **client を urllib で実装（依存ゼロ）** — ASGI アプリへの in-process round-trip test がやりにくい。
  httpx（既に dev 依存、ASGITransport で in-process テスト可）を `[api]` extra に加える。→ httpx 採用。

## Consequences

- Positive: S5 の read boundary が二言語（TS / Python）で確立し、interface 契約が凍結。API↔client の
  round-trip test で境界の drift を CI が検知。別プロセス / 別アプリ化の足場が揃う。
- Negative / trade-offs: `[api]` extra に httpx 追加（HTTP client のため妥当）。双方向 sync は未対応の
  まま（運用上は単一書き手で足りる）。
- Neutral: `repository-interfaces.md` を frozen に更新。`versioning-and-freeze.md` の freeze order #6 を
  frozen（read boundary 部分）に更新。

## Validation / Follow-up

- [x] `api/client.py:ViewerApiClient` + `tests/test_viewer_api_client.py`（round-trip / error code / 503）。
- [x] repository-interfaces.md を frozen に、stale な「schema draft 0.x」注記を 1.0 へ更新。
- [ ] write/sync 拡張（注文以外の write を HTTP に出す / 双方向同期）は use-case が固まったら別 ADR
  （認証 = ISSUE-0019 の PIN 再評価、衝突解決方針が前提）。
- [ ] desktop GUI を API client backed に差し替える（現状 desktop は local repository 直結で十分）。

## Related Files

- `docs/contracts/repository-interfaces.md` / `docs/contracts/versioning-and-freeze.md` / `shared-ids.md`
- `api/client.py` / `api/server.py` / `tests/test_viewer_api_client.py`
- `pyproject.toml`（`[api]` extra に httpx）

## Related Tests

- `tests/test_viewer_api_client.py`（API↔client round-trip） / `tests/test_viewer_api.py`

## Related Commits

- 本 ADR と同じ commit（S5 boundary）

## Supersedes / Superseded by

- Supersedes: —（関連: ADR-0004（contract-first boundary）/ ADR-0017（viewer API）/ ADR-0018（orders）/ ADR-0019（schema freeze））
- Superseded by: —
