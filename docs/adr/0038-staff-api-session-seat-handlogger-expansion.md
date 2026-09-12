# ADR-0038: staff API 拡張 — session/座席ライフサイクルと hand logger 遠隔制御

## Status

Proposed

## Date

2026-06-16

## Context

ADR-0037 で店舗操作を staff iPad アプリ（staff API 上のタッチ front-end）に統合する方針を確定した。
その際に **staff API のカバレッジを 4 つの優先機能（会計 / 注文 / セッション・座席 / ハンドロガー操作）
に突き合わせた**結果、以下が判明した:

| 優先機能 | 既存 staff API（ADR-0021 / 0026 / 0030） | 状態 |
|----------|------------------------------------------|------|
| 会計（Ledger/精算） | `POST .../ledger-entries`, `.../settlement/commit`, `PUT .../payment(-status)`, `GET .../settlement`, `GET /api/staff/buyin-presets` | ほぼ揃う（**reversal / point grant が欠落**） |
| 注文リクエスト捌き | `GET .../order-requests`, `POST .../confirm`, `.../reject`, `GET /api/menu` | 揃う |
| セッション/座席管理 | `POST /api/staff/players/merge` のみ | **大きく欠落**（session 作成/close, 座席割当, player 作成/rename が無い） |
| ハンドロガー操作 | なし | **欠落 + プロセス境界の難所**（hand logger は別プロセス常駐） |

現状、session 作成・座席割当・player 作成は **desktop GUI が repository を直接操作**しており、
HTTP に出ていない。hand logger の制御（新ハンド / ウィナー / リバイ）は **`main.py` の GUI/CLI と
同一プロセス内**で `IntegrationThread` に queue 投入する形でしか存在せず、`--ledger`（staff API を
in-process で抱えるプロセス）とは **別プロセス**である（ADR-0018 の単一書き手は ledger 系の話で、
hand logger プロセスは独立）。

関連: ADR-0037（staff app）/ ADR-0021（staff token / 単一書き手 / RLock）/ ADR-0018（in-process API）/
ADR-0008（hand logger × session write-through）/ ADR-0007（session 採番）/ ADR-0026（buy-in presets）/
ADR-0030（player merge）/ `docs/contracts/viewer-api.md` / `error-shapes.md`。

## Decision

staff API を **3 群**で additive に拡張する。すべて **staff token 必須**・既存 error code 再利用・
**単一書き手（write は `--ledger` プロセスのみ、read-only `--viewer-api` は 503）** を維持する
（ADR-0021 を踏襲）。実装は段階導入（A→B→C）とし、本 ADR は **設計のみ・コードなし**。

### A. 会計の不足分（reversal / point grant） — 最小・最優先

desktop GUI にあって staff API に無い 2 操作を additive に出す。`LedgerRepository` は RLock 済み
（ADR-0021）なのでロジック追加不要。

| method | path | need_write | 説明 | error 再利用 |
|--------|------|-----------|------|-------------|
| POST | `/api/staff/ledger-entries/{entry_id}/reverse` | yes | entry を reversal（append-only） | `not_found` / `invalid_amount` |
| POST | `/api/staff/players/{player_id}/point-grants` | yes | manual_grant（idempotency_key 任意） | `unknown_player` / `invalid_amount` / `duplicate_grant` |

### B. session / 座席 / player ライフサイクル — 座席タブを有効化

desktop が repo 直叩きしている操作を HTTP に出す。これらは `--ledger` プロセスが所有する
`SessionRepository` / `PlayerRepository` を mutate するため、**write 所有プロセスのみ**で有効。

| method | path | need_write | 説明 | error 再利用 |
|--------|------|-----------|------|-------------|
| GET  | `/api/staff/sessions` | no | 全 session 一覧（label/status/採番）。staff の卓選択用 | — |
| POST | `/api/staff/sessions` | yes | session 作成（label/blinds 任意、UUID4 採番 = ADR-0007） | `invalid_amount`（blinds 等） |
| POST | `/api/staff/sessions/{sid}/close` | yes | session を close | `not_found` / `already_closed` |
| GET  | `/api/staff/players` | no | registry 全 player（canonical, ADR-0030） | — |
| POST | `/api/staff/players` | yes | player 作成 | `empty_display_name` / `duplicate_display_name` |
| PUT  | `/api/staff/players/{pid}` | yes | display_name リネーム | `not_found` / `empty_display_name` / `duplicate_display_name` |
| PUT  | `/api/staff/sessions/{sid}/hands/{hid}/seats` | yes | 当該 hand の seat→player を **batch 設定**（assign_seat 群） | `not_found` / `session_closed` / `seat_taken` / `player_already_seated` / `unknown_player` / `invalid_seat` |

座席割当は session-seating の error（`docs/contracts/error-shapes.md` の session 節）を 1:1 で再利用する。
batch PUT は「その hand の seating を与えられた map で確定する」冪等な置換とする（ADR-0008 の
write-through と整合。`hand_id` は cross-app 複合キー `(session_id, hand_id)`, ADR-0006）。

### C. hand logger 遠隔制御 — プロセス境界を越えるため別途・後続

これは **最も risk が高く、A/B と性質が異なる**（hand logger は staff API と別プロセス・常駐録音）。
本 ADR では **方向性のみ提示し、確定は ISSUE-0020 の検証後**とする。staff app の「ハンド」タブは
**v1 では read-only**（`GET .../hands` で履歴閲覧のみ。ADR-0037 §5）。

提案する方向（採用は ISSUE-0020 で決定）:

1. **control-command queue（append-only）方式（第一候補）**: staff API は制御コマンド
   （`new_hand` / `winner` / `rebuy` 等 = 既存 `AudioEvent` 種別に対応）を共有ファイル
   （例 `logs/{session_id}.control.jsonl`）に **append** するだけ。hand logger プロセスが
   それを **tail して `audio_queue` に流す**。プロセス疎結合・ADR-0018 の単一書き手と矛盾しない
   （hand logger 状態の権威は hand logger プロセスのまま）。冪等性は command id で担保。
2. **hand logger が自前の control 受信を持つ**: `--cli`/GUI プロセスが小さな control 受信
   （HTTP or queue）を立て、staff API はそこへ proxy する。プロセス間結合が増える。
3. **当面は手動**: 録音 PC のオペレータが従来 GUI で操作し、iPad は閲覧のみ（最小 risk）。

いずれも **録音主体は PC**（ADR-0037 §4）を崩さない。hand logger 状態の二重書き込み（race）を
避けるため、制御は **常に hand logger プロセス経由**で適用し、staff API は「コマンドを伝えるだけ」
に徹する。

## Alternatives Considered

- **session/player 作成も desktop 専用のまま据え置く** — iPad だけで卓を回せず、結局 PC 操作が要る。
  座席タブが機能しない。→ B を追加。
- **B で seat 割当を単発（1 席ずつ）API にする** — 競合検証（seat_taken / player_already_seated）が
  部分適用で中途半端になりやすい。hand 単位の **batch 置換**が write-through（ADR-0008）と整合し
  冪等。→ batch PUT。
- **C を A/B と同時に出す** — hand logger とのプロセス間制御は race / 故障モードが多く、設計確定前に
  出すと hand logger（録音の本体）を不安定化させる risk。read-only 履歴で価値の大半は出せる。
  → C は ISSUE-0020 で検証後に分離。
- **hand logger 状態も staff API プロセスに移して一体化** — 録音スレッド・GameStateManager を
  `--ledger` プロセスへ移す大改修。ADR-0018 の所有境界と R 系（reconstruction）の前提を崩す。
  → 不採用（録音は PC・別プロセス維持）。

## Consequences

- Positive: 会計の reversal/grant が iPad から可能になり desktop 同等に。session/座席/player を
  HTTP で扱え、**iPad だけで卓のセットアップ〜会計が完結**（C を除く）。すべて additive・error 再利用で
  既存契約を壊さない。
- Negative / trade-offs: API 面が増え `viewer-api.md` 契約の保守対象が拡大。hand logger 遠隔制御（C）は
  プロセス境界の open question として残る（ISSUE-0020）。write は引き続き `--ledger` プロセス限定。
- Neutral: schema 変更なし（既存 model を read/write するのみ）。新 error code は不要（既存再利用）。
  `ViewerApiClient`（Python）にも対応 staff メソッドを additive 追加できる（ADR-0020/0021 と同形）。

## Validation / Follow-up

A/B は **実装済**（2026-06-16）。C は引き続き設計のみ（ISSUE-0020）。DoD:

- [x] A: `POST .../ledger-entries/{eid}/reverse` + `.../players/{pid}/point-grants` +
      `GET .../sessions/{sid}/ledger-entries`（reversal UI が取消対象を選ぶ read）+ staff client
      （`api/client.py`）+ `tests/test_viewer_api_staff_lifecycle.py`（error code 再利用を検証）。
      staff app の会計タブに **entry 一覧 + 取消（reversal）UI** を実装。
- [x] B: `GET/POST /api/staff/sessions`・`.../close`・`.../seating`・seat batch PUT、
      `GET/POST /api/staff/players`・`PUT .../players/{pid}` + tests（session-seating error の 1:1 再利用）。
      staff app 座席タブ（`staff/src/screens/SeatingTab.tsx`）を有効化。
- [ ] C: ISSUE-0020 で control-command 方式を spike → 別 worklog/必要なら追補 ADR で確定後に実装。
- [x] `docs/contracts/viewer-api.md` の staff write 節に A/B を追記。
- [x] CORS の `allow_methods` を GET/POST/PUT に拡張（web staff client の cross-origin write 用。
      LAN 限定 + token 認可前提。mobile 注文 POST も恩恵）。
- 補足: seat batch PUT は **append**（指定 hand に追記、conflict は error）。完全な idempotent-replace
      には core の「hand seating クリア」メソッドが要るため後続（ISSUE-0020）。ledger reversal は
      `GET .../ledger-entries`（list）の追加で staff app UI まで実装済。

## Related Files

- `api/server.py`（`/api/staff/...` に A/B endpoint 追加）/ `api/client.py`（staff メソッド追加）
- `core/ledger_repository.py`（reverse_entry / grant_points は実装済、公開のみ）
- `core/session_repository.py` / `core/player_repository.py`（create/close/assign/rename は実装済、公開のみ）
- `docs/contracts/viewer-api.md` / `docs/contracts/error-shapes.md`（再利用）
- hand logger 制御（C）: `main.py` / `integration/engine.py` / `output/`（control queue, 設計後）

## Related Tests

- `tests/test_viewer_api_staff.py`（A/B の round-trip + 認可 + error code、実装時に追加）

## Related Commits

- 本 ADR（設計のみ）と同じ commit

## Supersedes / Superseded by

- Supersedes: —（ADR-0037 の不足 API を具体化。ADR-0021 の staff write を additive 拡張。
  C の open question = ISSUE-0020）
- Superseded by: —
