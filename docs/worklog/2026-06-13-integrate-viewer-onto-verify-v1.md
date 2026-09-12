# 2026-06-13 — viewer API + mobile + 注文リクエストを verify-v1 ledger に重ねる統合

## Goal

2 つの並行ブランチを統合する。verify-v1（現行 = cash+point / entry_fee / settlement / points 残高の
ledger、PN5180 RFID 移行）を base とし、serene ブランチ（player 向け M1–M5）から **viewer API /
mobile / 注文リクエスト write path のみ**を取り込む。serene の cash-only ledger と M3 seat-selection
は **不採用**（verify-v1 の実装が canonical）。option A（user 決定）。

## Kept（serene から採用）

- **M1 viewer API**: `api/read_models.py` / `api/server.py`（read-only GET + 注文 endpoint、`[api]` extra）。
- **M2 mobile**: `mobile/`（Expo/RN, PlayerSelect→MySessions→MyHands→HandDetail + 会計 + 注文画面）。
- **M5 注文リクエスト**: `core/order_request.py` / `core/order_request_repository.py` / `core/menu.py` /
  `menu.json` + viewer API `/menu`・`/order-requests` + `gui/ledger_view.py` の確定/却下パネル。
- contracts: `player_session_summary` / `order_request` の schema + fixtures、`viewer-api.md`。

## Dropped（serene から不採用、verify-v1 で代替）

- serene の cash-only ledger（`core/ledger.py` / `core/ledger_repository.py` / その `ledger_entry`
  schema / `tests/test_ledger_repository.py`）→ verify-v1 の cash+point/settlement ledger を使用。
- serene の M3 seat-selection 改変（`gui/dashboard.py` / `main.py` プロンプト /
  `core/session_repository.py` / `core/game_state.py` / `tests/test_phase_m3_seat_selection.py`）→
  verify-v1 の `gui/seat_selection.py` + session/seat code を使用。
- serene の M4 ledger / M3 worklog、serene の `docs/contracts/ledger.md`、`gui/ledger_entry.py`。

## Renumbered（serene → 統合）

- serene ADR-0013（viewer API first）→ **ADR-0017**。
- serene ADR-0014（cash-only ledger first）→ **DROP**（verify-v1 ledger = ADR-0016 が置換）。
- serene ADR-0015（注文 write path）→ **ADR-0018**（ledger 参照を ADR-0016 に読み替え）。
- serene ISSUE-0013（viewer privacy）→ **ISSUE-0019**（verify-v1 の別 ISSUE-0013 と衝突回避）。
- 全 cross-reference（ADR 本体 / decision-log / CLAUDE.md / CHANGELOG / contracts / code コメント）を更新。

## Interface adaptations（verify-v1 ledger に合わせた）

verify-v1 の `LedgerRepository` は serene の cash-only 版と署名が異なるため以下を適合:

- `add_entry(session_id, player_id, kind, cash_amount=0, point_amount=0, note=None, hand_id=None,
  order: dict|None=None, occurred_at=None)`。`order` は **dict**（`{item_name, unit_amount, quantity}`）。
  注文確定は `order={...}` + `cash_amount=unit_amount*quantity`（verify-v1 の order_total ルール）。
- viewer の ledger summary は serene の `session_player_summary`（不在）の代わりに
  `compute_settlement(session_id)` を当該 player に絞って導出。field 名は `SessionSettlement` に揃える:
  `cash_in_total / order_total / entry_fee / point_spent_total / point_credited_total / net_due_to_store`
  （serene の `buy_in_total / adjustment_total / total_due` は廃止）。mobile types / fixtures / mock /
  MyLedgerScreen も同 shape に更新。
- `LedgerEntry.order` は属性ではなく **dict** のため、テストの `entry.order.item_name` →
  `entry.order["item_name"]`。
- verify-v1 ledger は **closed session への entry を拒否しない**（serene が想定した
  `LedgerSessionClosedError` は存在しない）。注文確定の closed ガードは
  `OrderRequestRepository.confirm_request` が担い `OrderSessionClosedError`（409 `session_closed`）を投げる。
- player 名前空間ずれ防止: `LedgerRepository` / `OrderRequestRepository` は、`player_repo` 未指定でも
  `session_repo` が与えられていればその `player_repo` を共有する（`SessionRepository.player_repo`
  プロパティを additive 追加）。これで test fixture（tmp players）が正しく解決される。
- `api/server.py`: `LedgerNotFoundError` を 404 `not_found` ハンドラに追加（compute_settlement の
  unknown session が ledger 例外で来るため）。`response_model=None` / `orders_writable` / 503 は維持。
- `main.py`: verify-v1 を base に `--viewer-api`（read-only 起動）を追加し、`--ledger` は
  `viewer_api.enabled=true` のとき uvicorn を背景スレッドで in-process 起動（orders_writable=True）。
  seat-selection プロンプトは再導入しない（verify-v1 が `gui/seat_selection.py` で担う）。

## Changed files（主なもの）

- 追加: `api/*`, `core/order_request*.py`, `core/menu.py`, `menu.json`, `mobile/*`,
  `docs/contracts/{viewer-api.md, schemas/player_session_summary.schema.json,
  schemas/order_request.schema.json, fixtures/player_session_summary/*, fixtures/order_request/*}`,
  `tests/test_viewer_*.py`, `tests/test_order_request_repository.py`,
  `docs/adr/0017-*.md`, `docs/adr/0018-*.md`, `docs/issues/0019-*.md`。
- 変更: `core/ledger_repository.py`（player_repo 共有 wiring）, `core/session_repository.py`
  （`player_repo` プロパティ）, `gui/ledger_view.py`（注文確定/却下パネル）, `main.py`,
  `config_default.json`, `tests/test_contracts.py`（`_MODELS` に 2 model 追加）,
  `docs/contracts/{error-shapes,repository-interfaces,validation-rules,README}.md`,
  `docs/decision-log.md`, `CLAUDE.md`, `CHANGELOG.md`, `.gitignore`, `docs/usage.md`。

## Test results

- `python -m pytest tests/ --ignore=tests/test_vision.py -q` → 476 passed, 0 skipped
  （verify-v1 baseline 430 + viewer/order/contracts 46）。
- `ruff check .` → clean。
- `cd mobile && npm run typecheck && npm test` → typecheck clean / tests 8 pass。
- Sanity: `python main.py --viewer-api` → `GET /api/menu` 200、注文 POST が read-only モードで
  503 `orders_unavailable`。

## Mismatches / judgment calls

- closed-session 注文確定ガードを order-request 層に置いた（verify-v1 ledger を変えないため）。
  serene の `test_confirm_failure_keeps_pending` を `OrderSessionClosedError` 期待に書き換え、
  「確定失敗で pending のまま + ledger に何も書かない」という回帰の意図は維持。
- ledger summary を settlement 由来にしたため、serene/mobile の summary field 名を全面更新。
  `total_due` は廃止し `net_due_to_store` を使用。
- serene の M1/M2/M5 個別 worklog は持ち込まず、本統合 worklog に集約（採番替えで参照が散らかるのを回避）。

## Remaining gaps

- 実機 E2E（`--ledger` + スマホ注文、`session_layer.enabled=true` での実データ）は未実施（Phase H）。
- order_request / player_session_summary schema の `1.0` freeze は ledger schema freeze と同時（未了）。
