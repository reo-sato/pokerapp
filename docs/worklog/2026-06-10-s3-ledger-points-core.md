# Worklog: S3 — session ledger + point ledger core 実装（ISSUE-0001 決着含む）

## Date

2026-06-10

## Scope / Task

Phase S3（ledger entries + point ledger）の WS0 契約 draft + WS1 core 実装。
ブロッカーだった ISSUE-0001（point 残高の source of truth）を ADR-0013 で決着させた上で、
S2 と同じ進め方（core 先行、desktop WS2 / mobile WS3 は後続）で実装する。

## Goal

- ISSUE-0001 の 4 論点（残高の計算者 / 中間集計の確定性 / 同一 session 内 grant→spend /
  grant 冪等性）を ADR で確定し Resolved にする。
- `ledger_entry` / `point_ledger_entry` の契約 draft（schema v0.1 + fixtures + 契約 doc）と
  core 実装（repository + validation + 永続化）を、業務ルール 1〜3・6・7 を enforce する形で
  完成させる。CI（pytest + ruff）緑。

## Changed Files

- `core/ledger.py` — 新規。`LedgerEntry` / `PointLedgerEntry` データクラス + kind/reason 定数。
- `core/ledger_repository.py` — 新規。`LedgerRepository`（add_entry / list_entries /
  session_totals / point_balance / grant_points / adjust_points / list_point_entries /
  plan_payment + 例外階層 + `ledger.json` 永続化）。
- `docs/adr/0013-s3-point-balance-fold-and-ledger-persistence.md` — 新規。ISSUE-0001 決着
  （fold 採用）+ 永続形・採番・`entry_fee` kind 追加・spend 同時生成・冪等性の決定。
- `docs/contracts/ledger-points.md` — 新規。S3 契約 draft（モデル・interface・永続形・freeze 状態）。
- `docs/contracts/schemas/ledger_entry.schema.json` — 新規（v0.1, draft）。
- `docs/contracts/schemas/point_ledger_entry.schema.json` — 新規（v0.1, draft）。
- `docs/contracts/fixtures/ledger_entry/*.json` — 新規（canonical / valid 2 / invalid 3）。
- `docs/contracts/fixtures/point_ledger_entry/*.json` — 新規（canonical / valid 2 / invalid 3）。
- `docs/contracts/error-shapes.md` — S3 error code 表を追記（planned 節を実装済へ差し替え）。
- `docs/contracts/validation-rules.md` — S3 節を planned から実装済へ昇格。
- `docs/contracts/repository-interfaces.md` — ledger interface（語彙非依存 + Python 具象）を追記。
- `docs/contracts/versioning-and-freeze.md` — freeze order #4 を draft + core 実装済に更新。
- `docs/contracts/README.md` — ディレクトリ構造に ledger-points / 2 schema / 2 fixtures dir を追記。
- `tests/test_ledger_repository.py` — 新規（13: CRUD / validation / 永続 roundtrip / 中間集計）。
- `tests/test_point_ledger.py` — 新規（5: ISSUE-0001 予告の回帰 4 本 + 同一 session 同居）。
- `tests/test_contracts.py` — `_MODELS` に 2 model 追加 + `test_core_ledger_matches_contract`。
- `docs/issues/0001-point-balance-source-of-truth.md` — Status: Resolved（Fix / Regression Test /
  Affected Files を実態へ更新）。
- `docs/decision-log.md` — ADR-0013 行追加、ISSUE-0001 行を Resolved に更新。
- `CLAUDE.md` — § Ledger & Points 新設（S3 昇格）、tree / 実装状況 / product scope /
  domain model / Business rules / Phase candidates / parallel plan / Phase 3 を更新。
- `CHANGELOG.md` — Unreleased に S3 節を追加。
- `.gitignore` — `ledger.json` を追加。

## Expected Behavior

- 残高は常に point entry 列の fold と一致し、front-end が残高・分割を再実装しない。
- entry fee への point 充当、残高超過の point 充当、order 明細不一致、closed session への
  追加、registry 非実在 player などはすべて error code 付きで reject。
- point 充当付き entry の追加で spend 系 point entry が atomic に併記される。
- 既存挙動（hand logger / S1 / S2 / R 系列）は不変。

## Implemented Behavior

期待どおり。設計上の補足:

- `kind` enum に `entry_fee` を追加（future scope 草案の 5 種 + 1。業務ルール 1 を ledger 上で
  enforce するため。ADR-0013 に根拠を記録）。
- `adjustment` は cash 専用（負値可・非 0）。point の補正は `adjust_points`（reason=adjustment,
  負残高化は reject）に分離した。
- 旧構想の `core/point_ledger.py` 単独ファイルではなく、ledger と point を 1 repository に
  統合（spend 同時生成を atomic にするため）。ISSUE-0001 の Affected Files 節も更新済み。
- session / player 系の検査は S2 の例外（`SessionNotFoundError` / `SessionClosedError` /
  `UnknownPlayerError`）を再利用し、error code の 1:1 対応を維持した。

## Test Results

- `pytest tests/ -q --ignore=tests/test_vision.py` — **349 passed**（新規 19 含む, skip 0）
- `ruff check .` — All checks passed!
- `pytest tests/test_contracts.py -q` — 緑（9 model: schema↔fixture + code↔contract）

## Mismatches Found During Testing

None observed.

## Fixes Applied

—（テストで mismatches なし）

## Remaining Gaps / Out-of-Scope

- [ ] WS2: desktop ledger 入力・中間集計の別画面（`gui/`、途中値の preview 表現を含む）。
- [ ] WS3: mobile ledger 画面（mock, fixtures ベース）。
- [ ] schema `1.0` freeze（上流 session schema freeze = ISSUE-0005 決着後。S2 と同じ運用）。
- [ ] S4: settlement（`result_credit` 付与フローと idempotency_key の規約化を含む）。
- [ ] 残高 fold の derived cache（必要になった場合のみ。契約不変で additive に追加可能）。

## Related ADRs

- `docs/adr/0013-s3-point-balance-fold-and-ledger-persistence.md` — 本タスクの中心決定。
- `docs/adr/0003-...` / `docs/adr/0007-...` — 前提（domain 拡張 / S2 永続化スタイル）。

## Related Issues

- `docs/issues/0001-point-balance-source-of-truth.md` — 本タスクで Resolved。
- `docs/issues/0005-s2-session-seating-freeze-blockers.md` — S3 schema `1.0` 昇格の上流 blocker。

## Related Commits

- S3 core 実装コミット（本 worklog と同一コミット）
