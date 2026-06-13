# ADR-0016: Ledger / points / settlement design direction (S3)

## Status

Accepted

## Date

2026-06-10

## Context

hand logger core / S1 player registry / S2 session + hand-based seating（`core/session*.py`,
ADR-0006/0007）/ S2.x hand-logger 接続戦略（ADR-0008）が揃い、session world は「テーブル単位の
現実進行」の source of truth になった。一方で **お金（残高）/ ポイント / 精算** の source of truth は
未確定のままで、これは S3 のブロッカー（ISSUE-0001）として登録されていた。

S3 では session world の上に **ledger（金銭イベント）/ points（店内ポイント残高）/ settlement
（session 締めの精算）** を載せる。論点・制約（forces）:

- 想定ユーザは **home game host**（小規模クラブ / 個人配信）。「混乱しないレベルの帳簿」が要件で、
  複式簿記・厳密会計・税務は過剰（外部ツール領域）。
- **PHH / hand logger JSON は外向き契約であり read-only**（ADR-0008 immutability）。ledger は
  これを壊さない。
- hand logger の **chip 単位** と ledger の **cash（円）単位** は別物。自動換算（rate / rake）は
  future scope で、S3 では行わない。
- CLAUDE.md § Domain model は既に `ledger_entry`（cash+point 併用）/ `point_ledger_entry`（grant/spend）/
  `session_settlement`（player→店）の概念モデルと business rules（entry fee cash only 等）を planned で
  定義している。S3 はこれを実装可能な契約へ落とす。
- **残高の source of truth**（ISSUE-0001）: fold（ledger 走査）/ cached カラム / session 繰越テーブルの
  3 択が未決で、これが決まらないと残高 API 契約を freeze できない。
- 永続レイアウト: ADR-0007 は「将来 ledger を `sessions[].ledger[]` に additive で同居させる」可能性を
  挙げていた。一方、ledger app は将来 **別アプリ化（S5）** が planned。

関連: CLAUDE.md（§ Domain model / Business rules / Phase 3）、ADR-0003（domain 拡張）、
ADR-0007（session 永続・採番）、ADR-0008（hand-logger 接続）、ISSUE-0001（残高 source of truth）。

## Decision

S3 の ledger / points / settlement を、以下の **軽量・append-only な 2 台帳 + derived settlement**
として設計する（実装は S3.1〜S3.3、本 ADR は方針確定）:

1. **抽象度は「home game host 帳簿」**。複式簿記は採用しない。ただし訂正は **reverse / adjustment
   entry**（append-only）で表し、mutate / delete はしない。
2. **2 台帳モデル**（CLAUDE.md の 2 エンティティ形を採用・refine）:
   - `ledger_entry` = 金銭/価値イベント 1 件。`cash_amount` + `point_amount` を同一行に持つ
     （buy_in/rebuy/add_on/order は併用可）。CLAUDE.md の `kind` enum に **`entry_fee` を additive
     追加**し（cash only 制約）、全金銭イベントを 1 台帳に集約する。
   - `point_ledger_entry` = ポイント増減 1 件。**point 残高の権威台帳**。
3. **単一台帳（per-entry の medium/currency 判別）案は採らない**。host は「1 buy_in = 1 行」で読みたく、
   CLAUDE.md の既存モデルとも整合する。per-currency 完全分割も採らない（行数増・cross-link 増）。
4. **金額は整数円**（1 = ¥1, 小数なし）、単一通貨 JPY 暗黙。**point は整数点**。chips とは無関係で
   自動換算しない。`currency` は将来 additive フィールドとして予約。
5. **point 残高 = `point_ledger_entry.delta_points` の fold**（ISSUE-0001 の選択肢 A）。cached カラムを
   権威にしない。cache は後で導出として足してよいが、fold が正。`ledger_entry.point_amount` は
   point_ledger_entry のミラー（整合は core が enforce）。
6. **settlement は derived materialized view**。`net_due_to_store = 符号付き Σ cash_amount`(当該
   session・player)。closed session のみ確定（commit）し、`(session, player)` 1 行を凍結。
   `payment_status`（paid/unpaid, partial なし）が唯一の可変フィールド。常に **player→店** の 1 方向。
   open 中の中間集計は **speculative（途中値）** として UI 区別。
7. **永続は別ストア `ledger.json`**（`{schema_version, ledger_entries, point_ledger_entries,
   settlements}`、アトミックリネーム、`.gitignore`）。`players.json` / `sessions.json` と同パターン。
   将来の **別アプリ化（S5）** に向け session ストアと疎結合にする。これは ADR-0007 の
   `sessions[].ledger[]` 同居案を **refine（不採用）** するもの（ADR-0007 の session 永続・採番判断
   自体は不変）。
8. **auto events は採らず manual-first**。host が UI から ledger entry を入力する。hand logger は
   ledger entry を自動生成しない。`ledger_entry.hand_id`（rake/fee）/ `result_credit`（session 結果→
   point）は schema 上のフックに留め、S3 では未配線。
9. **契約は本 phase では markdown draft**（`ledger-overview.md` / `ledger-schema.md` 擬似 schema）。
   実 `schemas/*.schema.json` + fixtures + `tests/test_contracts.py` 登録 + core repository は
   **S3.1（ISSUE-0016）** で追加し、その時点で freeze 手続きに入る。

## Alternatives Considered

- **Alternative A（採用）— 2 台帳（event ledger + point ledger）+ derived settlement / 整数円 /
  別ストア / fold balance / reversal 訂正 / manual-first**
  - Pros: CLAUDE.md の既存ドメインモデルと整合。host は 1 イベント 1 行で読める。append-only で監査性が
    高く repo の既存思想（json_writer / event sidecar / S1・S2 repository）と一致。別ストアで将来の
    別アプリ化が容易。fold balance は ISSUE-0001 の監査性要件を満たす。
  - Cons: fold は entry 増で走査コスト増（cache は後付け）。ledger_entry.point_amount と
    point_ledger_entry の整合を core が担保する必要。
  - Why chosen: 「軽量・現実的・既存契約と整合・将来分割可能」を最短で満たす。

- **Alternative B — 単一台帳（per-entry medium/currency 判別フィールド）**
  - Pros: balance = medium で filter する単純さ。台帳が 1 つ。
  - Cons: cash+point 併用 buy_in が 2 行に割れ、1 イベント 1 行の host 直感に反する。CLAUDE.md の
    2 エンティティモデルを書き換える必要。
  - Why rejected: home host の読みやすさと既存 spec 整合を優先（user 確認済）。

- **Alternative C — cash 台帳と point 台帳を完全分離（each single-amount）**
  - Pros: 関心の最大分離。
  - Cons: 1 イベントあたり行数が最も増え、cross-ledger リンクが増える。
  - Why rejected: 軽量帳簿の狙いに対し over-engineering。

- **Alternative D — cached `current_point_balance` を権威にする（ISSUE-0001 選択肢 B）**
  - Pros: 残高読み出しが速い。
  - Cons: ledger との二重更新で不整合リスク（更新失敗時）。append-only 思想と相反。
  - Why rejected: 監査性・整合性を優先。cache は将来の最適化として fold から導出する形に留める。

- **Alternative E — `sessions[].ledger[]` に同居（ADR-0007 のヒント踏襲）**
  - Pros: ファイル数が少なく seating と co-locate。
  - Cons: ledger が session ストアに結合し、将来の別アプリ化（S5）で切り出しづらい。sessions.json 肥大。
  - Why rejected: 別アプリ化 planned のため疎結合な別ストアを優先。

- **Alternative F — 複式簿記（double-entry）/ 厳密会計**
  - Pros: 会計的に厳密。
  - Cons: home host には過剰、学習コスト・実装コスト大。
  - Why rejected: スコープ過剰。ただし「逆仕訳（reversal entry）」の利点だけは取り込む。

- **Alternative G — auto events（hand 結果から ledger を自動起票）**
  - Pros: 入力手間が減る。
  - Cons: chips↔円換算（rate/rake）が必要で未確定。誤起票リスク。PHH 接続の複雑化。
  - Why rejected: S3 は manual-first。auto は schema フックを残し future phase へ。

## Consequences

- Positive
  - お金 / ポイント / 精算の source of truth が確定（fold balance、derived settlement、別ストア）。
    ISSUE-0001 の中心問題が解ける。
  - hand logger / PHH を無改修のまま ledger を載せられる（read-only）。
  - append-only で監査・訂正（reversal）が一貫。repo の既存永続思想と揃う。
  - 別ストアにより S5 の別アプリ化・API 化が容易。
- Negative / trade-offs
  - fold balance は entry 増で走査コスト増（cache は後付け最適化に回す）。
  - `ledger_entry.point_amount` と `point_ledger_entry` の二重表現を core が整合させる責務を負う。
  - session_id が hand logger（timestamp）と session レイヤ（UUID）で並存する既存課題（ADR-0007）は
    ledger でも `(session_id)` を session レイヤ側に揃えることで回避（hand logger 接続は ADR-0008 経由）。
- Neutral / new constraints
  - ledger freeze は session schema freeze（#3）の後（依存順）。settlement（#5）は S3.3 で derived view +
    export を先行し、schema freeze は S4。
  - 金額は整数円固定。多通貨・小数は将来 additive（`currency`）。

## Validation / Follow-up

- [x] `docs/contracts/ledger-overview.md`（ドメイン / invariants / interface 草案）を追加。
- [x] `docs/contracts/ledger-schema.md`（擬似 schema v0.1 + versioning + 拡張余地）を追加。
- [x] `error-shapes.md` / `validation-rules.md` に ledger セクションを additive 追記。
- [x] ISSUE-0001 を更新（残高 = fold の方針確定、残サブ問題を S3.1/S3.2 へ）。
- [x] ISSUE-0016（S3.1 core）/ ISSUE-0017（S3.2 desktop viewer）/ ISSUE-0018（S3.3 settlement export）を起票。
- [x] `versioning-and-freeze.md` freeze order #4/#5、`README.md`、CLAUDE.md、decision-log を更新。
- [x] **S3.1（実装済）**: 実 `schemas/{ledger_entry,point_ledger_entry,session_settlement}.schema.json` +
  fixtures + `tests/test_contracts.py::_MODELS` 登録、`core/ledger.py` + `core/ledger_repository.py`、
  `ledger.json` + `.gitignore`、invariants/validation、code↔contract test
  （`tests/test_ledger_repository.py`, ISSUE-0016）。
- [ ] **S3.2**: desktop ledger viewer/editor（別画面, `gui/dashboard.py` は触らない）（ISSUE-0017）。
- [ ] **S3.3**: settlement compute + paid/unpaid + CSV export（ISSUE-0018）。
- [ ] ledger schema を `1.0` へ freeze（ISSUE-0001 残項目 + session freeze 後）。

## Related Files

- `docs/contracts/ledger-overview.md`, `docs/contracts/ledger-schema.md`
- `docs/contracts/error-shapes.md`, `docs/contracts/validation-rules.md`,
  `docs/contracts/versioning-and-freeze.md`, `docs/contracts/README.md`
- `docs/issues/0001-point-balance-source-of-truth.md`,
  `docs/issues/0016-ledger-core-implementation.md`,
  `docs/issues/0017-ledger-desktop-viewer.md`, `docs/issues/0018-settlement-export.md`
- （planned, S3.1）`core/ledger.py`, `core/ledger_repository.py`,
  `docs/contracts/schemas/{ledger_entry,point_ledger_entry,session_settlement}.schema.json`,
  `docs/contracts/fixtures/...`, `.gitignore`（`ledger.json`）

## Related Tests

- （planned, S3.1）`tests/test_ledger_repository.py`,
  `tests/test_contracts.py::test_fixtures_match_schema`（ledger 3 model 登録後）

## Related Commits

- 本 ADR と同じコミット（S3 planning, docs-only）

## Supersedes / Superseded by

- Supersedes: ADR-0013（S3 point 残高=fold + 永続化の決定。verify-v1 マージで実装・契約を本 ADR の版へ
  一本化。fold 採用は ADR-0013 を踏襲し、settlement / desktop viewer / CSV export を追加）
- Superseded by: —
- 関連: ADR-0003（domain 拡張）、ADR-0007（session 永続・採番。`sessions[].ledger[]` 同居案を本 ADR が
  refine = 別ストア採用）、ADR-0008（hand-logger 接続・immutability）、ISSUE-0001（残高 source of truth）。
