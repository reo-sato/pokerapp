# Issue 0001: point ledger の残高計算と source of truth が未確定

## Date

2026-05-22

## Status

Resolved（2026-06-10, ADR-0013: A 案 = point_ledger_entry の fold を採用。S3 core 実装済）

## Severity / Priority

- Severity: High（S3 着手前にブロッカー化する可能性）
- Priority: P1

## Area

spec / future-scope: point ledger / session ledger / store settlement

## Expected Behavior

- player ごとの **prize point 残高** が常に一意に定まり、buy-in / rebuy / add-on / order の
  point 充当時に「使える点数の上限」が明確になる。
- 残高計算の **source of truth** が単一であり、UI 表示・ledger entry の検証・session 集計の
  すべてが同じ値を参照する。
- point 不足時、cash 補完への分岐ロジックが残高判定に依存する（CLAUDE.md § Business rules 参照）。

## Actual Behavior

未実装。現時点で point ledger 残高をどこに置くかは未確定で、以下のような選択肢が
すべて open question として残っている。

- A. `point_ledger_entry` を全件 fold して残高を都度算出する（source = ledger そのもの）。
- B. `player` テーブルに `current_point_balance` カラムを持ち、ledger_entry 追加時に同期更新する。
- C. session 単位の集計テーブルに残高を持ち、session を跨ぐ場合は専用 ledger entry で繰越する。

それぞれで以下のトレードオフが残っている。

- A は監査性が高いが、entry 件数が増えると残高表示のたびに走査コストが大きくなる。
- B は読み出しが速いが、ledger との不整合（更新失敗時の二重書き込み等）のリスクがある。
- C は session 終了時の確定処理と整合させやすいが、繰越 entry の自動生成ルールが必要。

## Reproduction

仕様レビューにより以下が未決と確認:

1. `point_ledger_entry` を追加した直後、UI に表示する残高は誰が計算するか？
2. session 中間集計を player に提示する際、その時点での point 残高は確定値か途中値か？
3. session 終了時に `result_credit` で point を付与する際、それは同 session 内の
   buy-in に充当可能か（同一 session で grant → spend が同居できるか）？
4. `manual_grant` と `campaign_grant` の冪等性（重複 grant 防止）はどこで担保するか？

## Root Cause

S0（spec expansion）時点では point ledger の物理レイアウト（DB / JSON / in-memory）が
確定していないため、source of truth の置き場所を決定できない。S3（ledger 実装）の ADR で
解決する必要がある。

## Fix

**ADR-0013 で決着（S3 core 実装済）**:

- 残高計算の正規アルゴリズムは **fold（A 案）**: `point_balance(player_id)` =
  当該 player の `point_ledger_entry.delta_points` の総和。計算者は core
  （`LedgerRepository`）のみで、front-end は結果を表示するだけ（Reproduction 論点 1）。
- `current_point_balance` は **物理化しない**（B 案棄却: 全件メモリロード方式では
  二重書き込みの整合リスクだけが残る。将来必要なら derived cache を additive に追加）。
- 残高は **player に global**（session を跨ぐ）。繰越 entry 不要（C 案棄却）。
  同一 session 内の `result_credit` → buy-in 充当の同居は **可**（fold は記録順,
  Reproduction 論点 3）。
- 冪等性は grant 系の任意 `idempotency_key` の一意性で担保（重複 → `duplicate_grant`,
  Reproduction 論点 4）。
- session 中間集計（`session_totals`）は **途中値・非確定**（確定は S4 settlement,
  Reproduction 論点 2）。UI 上の preview 表現は WS2/WS3 着手時に確定する。
- 付随決定: point 不足は strict reject（`insufficient_points`）+ `plan_payment` による
  cash 補完分割、spend 系 point entry は core が ledger entry から同時生成、
  永続化は専用 `ledger.json`。詳細は ADR-0013 / `docs/contracts/ledger-points.md`。

## Regression Test

実装済（`tests/test_point_ledger.py`）:

- `test_balance_matches_fold_of_entries` — 残高 = fold の一致
- `test_insufficient_points_falls_back_to_cash` — strict reject + `plan_payment` の cash 補完
- `test_entry_fee_rejects_points` — entry fee は cash only
- `test_grant_idempotency` — manual/campaign grant の重複防止
- `test_grant_and_spend_in_same_session` — 同一 session 内 grant→spend 同居（論点 3）

## Affected Files

- `core/ledger.py` / `core/ledger_repository.py`（`core/point_ledger.py` 構想は
  ledger と point を 1 repository に統合する形に変更）
- `docs/contracts/ledger-points.md` / `docs/contracts/schemas/{ledger_entry,point_ledger_entry}.schema.json`

## Related Worklog

- `docs/worklog/2026-05-22-spec-expand-session-ledger-scope.md`
- `docs/worklog/2026-06-10-s3-ledger-points-core.md`（決着・実装）

## Related ADRs

- `docs/adr/0003-expand-domain-from-hand-logging-to-session-ledger-and-store-settlement.md`
- `docs/adr/0013-s3-point-balance-fold-and-ledger-persistence.md`（決着）

## Related Commits

- 本 issue と同じコミット（spec expansion phase）

## Notes

関連する未確定事項（本 issue では扱わないが、別 issue を起こす候補）:

- "cash-out" という語の現仕様での意味（point 換金 vs cash 引き出し vs settlement 完了）が曖昧。
- hand logger と ledger app の参照同期方式（pull / push / event bus）の選定。
- session 中間集計の確定タイミングと UI 上の確定／途中値の区別表現。

これらは S2〜S5 着手前に追加 issue として起こす。
