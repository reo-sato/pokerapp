# Worklog: S3 planning — ledger / points / settlement layer 設計

## Date

2026-06-10

## Scope / Task

S3（ledger / points / settlement）の **設計 + contracts docs 確定**（実装は次フェーズ）。お金・
ポイント・精算の source of truth を session / hand world の上にどう載せるかを決め、ドメインモデル /
invariants / JSON schema 草案 / ADR / 実装 issue を起こす。**docs-only**（`.py` / 実 schema / fixtures は
未変更）。

## Goal

- ledger / points / settlement layer の **役割とスコープ**（重さ）を文書で確定する。
- 主要エンティティと invariants を `ledger-overview.md` に整理する。
- JSON schema 草案を `ledger-schema.md` にまとめる（freeze の下地）。
- ADR を 1 本起こし「なぜこの重さ / モデルか」を説明する。
- S3 実装フェーズ（S3.1 / S3.2 / S3.3）の issue を立てる。
- 既存 docs（CLAUDE.md / decision-log / ISSUE-0001 / 0005 / freeze order）と整合させる。

## 想定ユースケース（home game host 前提）

- 小規模クラブ / 個人配信の host が、1 卓 1 session の **buy-in / rebuy / add-on / 注文 / entry fee** を
  player ごとに記録し、session 終了時に「各 player が **店に** いくら払うか（net due to store）」と
  paid/unpaid を確定する。
- 店内 **ポイント**（prize point）を grant / spend し、buy-in 等に充当（不足は cash 補完）。
- 「今日のキャッシュフロー」「ある player の過去 N 回の収支」を CSV で家計簿 / Excel 再集計。

## ledger / points layer の役割と非目標

- 役割: player→**店** の cash 債務、player の **point 残高**、session 締めの **settlement** を記録。
- 非目標: 複式簿記 / 厳密会計 / 税務、決済手段連携（銀行 / QR / blockchain）、player 間精算、
  online gambling 規制。PHH / hand logger JSON は **read-only**。chips ≠ 円で自動換算しない。

## 候補設計パターンと採否（詳細は ADR-0016）

- **2 台帳（event ledger + point ledger）+ derived settlement** … **採用**（A）。CLAUDE.md の既存
  ドメインモデルと整合し、1 イベント 1 行で host が読みやすい。
- 単一台帳（per-entry medium 判別） … 却下（B）。cash+point 併用が 2 行に割れる / 既存 spec 改変。
- cash/point 完全分割 … 却下（C）。over-engineering。
- cached balance を権威 … 却下（D, ISSUE-0001 選択肢 B）。append-only 思想・整合性を優先し
  **fold balance**（A）を採用。cache は将来 fold から導出。
- `sessions[].ledger[]` 同居（ADR-0007 ヒント） … 却下（E）。別アプリ化（S5）に向け **別ストア
  `ledger.json`** を採用（ADR-0007 の同居案を refine、session 永続判断自体は不変）。
- 複式簿記 … 却下（F）。ただし **逆仕訳（reversal entry）** の利点のみ採用（append-only 訂正）。
- auto events（hand 結果→自動起票） … 却下（G）。**manual-first**。chips↔円換算が未確定のため schema
  フック（`hand_id` / `result_credit`）だけ残し future phase。

確定した 3 つの fork（user 確認済）:
1. **2 台帳モデル**（cash_amount + point_amount を持つ event ledger ＋ 権威 point ledger）。
2. **整数円**（1=¥1, 小数なし）、単一 JPY 暗黙、point は整数点。`currency` は将来 additive。
3. **別ストア `ledger.json`**（players/sessions と同パターン）。

## 採用ドメインモデル（主要エンティティ）

- `ledger_entry`（金銭イベント, append-only, cash+point 併用, `kind` に `entry_fee` を additive 追加）。
- `point_ledger_entry`（ポイント増減, **残高の権威台帳**, fold が source of truth）。
- `session_settlement`（`(session, player)` 1 行, entries の derived materialized view, close で確定,
  `net_due_to_store = 符号付き Σ cash_amount`, paid/unpaid, player→店）。

## 主要 invariants（`ledger-overview.md` に明文化）

append-only / point 残高 = fold / 残高非負 + cash 補完 / ledger↔point 整合 / 参照整合 /
entry fee cash only / 非ゼロ移動 / settlement derived・player→店 / PHH 不変・単位分離（計 9）。

## Changed Files

- `docs/contracts/ledger-overview.md`（新規）— ドメイン / invariants / interface 草案 / error / migration / freeze 状態。
- `docs/contracts/ledger-schema.md`（新規）— 3 model の擬似 JSON Schema（v0.1）/ versioning / 拡張余地 / `ledger.json` 形。
- `docs/adr/0016-ledger-points-settlement-design-direction.md`（新規, Accepted）— 抽象度 / 2 台帳 / 整数円 / 別ストア / fold balance / reversal / manual-first と alternatives A–G。
- `docs/issues/0016-ledger-core-implementation.md`（新規）— S3.1 core schema + repository。
- `docs/issues/0017-ledger-desktop-viewer.md`（新規）— S3.2 desktop 別画面 viewer/editor。
- `docs/issues/0018-settlement-export.md`（新規）— S3.3 settlement 確定 + CSV export。
- `docs/issues/0001-point-balance-source-of-truth.md`（更新）— 残高 = fold の方針確定、残サブ問題を S3.1/S3.2 へ。
- `docs/issues/0005-s2-session-seating-freeze-blockers.md`（更新）— ledger freeze（#4）は session freeze（#3）依存の注記。
- `docs/contracts/error-shapes.md`（更新）— ledger / settlement error code セクションを実体化。
- `docs/contracts/validation-rules.md`（更新）— ledger / points / settlement の validation を決定方針で更新。
- `docs/contracts/versioning-and-freeze.md`（更新）— freeze order #4/#5 の状態更新。
- `docs/contracts/README.md`（更新）— ディレクトリツリーに ledger-overview / ledger-schema 追加。
- `docs/decision-log.md`（更新）— ADR-0016 / ISSUE-0016〜0014 を index 追加、ISSUE-0001 行を更新。
- `CLAUDE.md`（更新）— S3 planning 結果 + S3.1/S3.2/S3.3 phasing + 実装状況表 / Phase 3 / freeze order。
- `CHANGELOG.md`（更新）— Unreleased に S3 planning エントリ。

## Expected Behavior

- docs のみで ledger/points/settlement の設計・契約が確定し、`.py` / 実 schema / fixtures / `ledger.json` は不変。
- `pytest` は不変（実 schema を `_MODELS` に未登録のため contract test も不変）。

## Implemented Behavior

- 上記 Changed Files の通り docs を追加・更新。コード・テスト・実 schema・fixtures・`.gitignore` は未変更。
- ISSUE-0001 の中心問題（残高 source of truth）を **fold（選択肢 A）** で決着させ、ADR-0016 に記録。

## Test Results

- `pytest tests/ -v --ignore=tests/test_vision.py` — docs-only のため不変を確認（回帰なし）。
- Manual: cross-reference 監査（新規ファイルパス / ADR・ISSUE 番号の一意性 / freeze order 整合 /
  entity・field の overview↔schema↔CLAUDE.md↔ADR 整合）。

## Mismatches Found During Testing

- なし（docs-only）。設計上の注意点として、`ledger_entry.point_amount` と `point_ledger_entry` の
  二重表現の整合を core が担保する必要を ADR-0016 / invariants に明記した。

## Fixes Applied

- 該当なし（planning フェーズ）。

## Remaining Gaps / Out-of-Scope

- [ ] 実 `schemas/*.schema.json` + fixtures + `tests/test_contracts.py::_MODELS` 登録（S3.1, ISSUE-0016）。
- [ ] `core/ledger.py` / `core/ledger_repository.py` / `ledger.json` + `.gitignore`（S3.1）。
- [ ] desktop ledger viewer/editor（S3.2, ISSUE-0017）。
- [ ] settlement 確定 + paid/unpaid + CSV export（S3.3, ISSUE-0018）。
- [ ] ISSUE-0001 残: idempotency_key 運用 / speculative の UI 表現（S3.1/S3.2）。
- [ ] ledger schema `1.0` freeze（session freeze #3 + ISSUE-0001 残決着後）。settlement freeze は #5/S4。
- [ ] auto events（hand 結果→entry）/ chip↔円 rate / rake / fee / 決済連携 / mobile UX / sync（future）。

## Related ADRs

- `docs/adr/0016-ledger-points-settlement-design-direction.md` — 本 planning の中心判断。
- `docs/adr/0007-s2-session-layer-persistence-and-id-issuance.md` — `sessions[].ledger[]` 同居案を refine。
- `docs/adr/0003-expand-domain-from-hand-logging-to-session-ledger-and-store-settlement.md` — domain 拡張の源流。

## Related Issues

- `docs/issues/0001-point-balance-source-of-truth.md` — 残高 = fold で方針確定。
- `docs/issues/0016-ledger-core-implementation.md` / `0017-ledger-desktop-viewer.md` / `0018-settlement-export.md`。
- `docs/issues/0005-s2-session-seating-freeze-blockers.md` — ledger freeze の依存順。

## Related Commits

- 本 worklog と同じコミット（S3 planning, docs-only）。
