# Worklog: M4 (= S3a) — cash-only ledger（記録・スタッフ入力・会計参照）

## Date

2026-06-11

## Scope / Task

プレイヤー向け参照アプリのロードマップ M4（ADR-0013）。S3 を ADR-0014 で S3a/S3b に分割し、
S3a = cash-only ledger を WS0（契約）/ WS1（core）/ WS2（desktop 入力）/ WS3（mobile 参照）+
viewer API の 4 面で実装。ISSUE-0001（point 残高）には依存しない。

## Goal

- buy-in / rebuy / add-on / 注文 / 調整を open session に記録でき、player ごとの中間集計
  （確定値ではない）が desktop / API / mobile で見える。
- point は一切受け付けない（`points_not_supported`）。既存機能の挙動は不変。

## Changed Files

- `docs/adr/0014-s3a-cash-only-ledger-first.md` — S3 分割・schema draft 0.x・永続形・validation の決定
- `docs/contracts/ledger.md` — ledger_entry model / 中間集計 / interface / write の所在
- `docs/contracts/schemas/ledger_entry.schema.json`（0.1, if/then で order 明細の有無を kind に連動）
  + `docs/contracts/fixtures/ledger_entry/`（canonical / valid 2 / invalid 5）
- `docs/contracts/{error-shapes,validation-rules,repository-interfaces,viewer-api,README}.md` — ledger 追記
- `tests/test_contracts.py` — `_MODELS` に `ledger_entry`
- `core/ledger.py` / `core/ledger_repository.py` — LedgerEntry/OrderDetail + repository
  （validation source of truth, `ledger.json` append-only, アトミックリネーム）
- `api/read_models.py` / `api/server.py` — `get_player_session_ledger` + `/ledger` endpoint
  （`create_app` に `ledger_repo` DI）
- `gui/ledger_entry.py` / `main.py` — スタッフ用入力画面 + `--ledger` フラグ
- `mobile/src/api/{types,repository,httpRepository,mockRepository}.ts` /
  `mobile/src/mocks/fixtures.ts` / `mobile/src/screens/{MyLedgerScreen,MyHandsScreen}.tsx` /
  `mobile/App.tsx` — 会計画面（read-only, mock/HTTP 両実装）
- `.gitignore`（ledger.json）/ `CLAUDE.md`（§ Ledger 新設・実装状況・Phase 3 分割・コマンド）/
  `docs/usage.md`（会計入力）/ `mobile/README.md` / `CHANGELOG.md`
- `tests/test_ledger_repository.py`（新規 13）/ `tests/test_viewer_api.py`（ledger endpoint +
  404 ケース追加）

## Expected Behavior

- スタッフが `--ledger` で open session に entry を追加 → 中間集計と履歴が画面に出る。
- プレイヤーが mobile「会計」で自分の entries + summary を見る（他人の entry は混ざらない）。
- closed session への追記 / point 指定 / 金額規則違反 / 未登録 player は error code で reject。

## Implemented Behavior

Expected どおり。補足:

- 注文の金額は GUI 側で単価×数量から自動計算（core の `cash_amount == unit_amount×quantity`
  検証と二重化しない入力 UX）。
- `add_entry` の order 明細は 3 つ揃い必須（部分指定は `invalid_amount`）。
- unknown session は session 側の `SessionNotFoundError`（code: not_found）を透過。

## Test Results

- `pytest tests/ -q --ignore=tests/test_vision.py` — **378 passed**（+16、skip 0）
- mobile: `npm run typecheck` クリーン / `npm test` **7 pass**（getPlayerLedger の summary が
  core 定義と一致することを含む）/ `npx expo export --platform web` 成功
- 手動: GUI 画面（customtkinter）は headless のため実機スモーク待ち（M3 と同様）

## Mismatches Found During Testing

None observed.

## Fixes Applied

—

## Remaining Gaps / Out-of-Scope

- [ ] M5: player スマホからの注文 write path（order-request → スタッフ確定。ISSUE-0013 決着が前提）
- [ ] M6 (= S3b): ISSUE-0001 決着 ADR → point_ledger_entry → `points_not_supported` 解除 →
  ledger schema `1.0` freeze
- [ ] S4: settlement（close 後の訂正手段は S4 まで無い — adjustment も open 中のみ）
- [ ] `--ledger` 画面の実機スモーク（運営 PC）
- [ ] menu master（品名・単価のプリセット）は未導入（毎回手入力。M5 で注文契約と一緒に検討）

## Related ADRs

- `docs/adr/0014-s3a-cash-only-ledger-first.md` / `docs/adr/0013-player-facing-viewer-api-first-architecture.md`

## Related Issues

- `docs/issues/0001-point-balance-source-of-truth.md`（Open のまま — S3b gate）/
  `docs/issues/0013-player-viewer-privacy-model.md`

## Related Commits

- （本 commit。M1 = `b5eadc8`, M2 = `ba0ad46`, M3 = `9f6661f`）
