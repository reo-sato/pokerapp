# Issue 0001: point ledger の残高計算と source of truth が未確定

## Date

2026-05-22

## Status

Open

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

未確定。S3 着手時に専用 ADR を起こし、以下を決める:

- 残高計算の正規アルゴリズム（fold vs cached）。
- `current_point_balance` を物理化するか（する場合は再計算ジョブの設計）。
- session を跨ぐ point の繰越方式と、冪等性キーの定義。
- session 中間集計で「確定値ではない」ことの UI 表現（speculative/preview ラベル等）。

## Regression Test

未実装。S3 実装時に以下を追加する想定:

- `tests/test_point_ledger.py::test_balance_matches_fold_of_entries` — cached/fold 整合性
- `tests/test_point_ledger.py::test_insufficient_points_falls_back_to_cash` — cash 補完
- `tests/test_point_ledger.py::test_entry_fee_rejects_points` — entry fee は cash only
- `tests/test_point_ledger.py::test_grant_idempotency` — manual/campaign grant の重複防止

## Affected Files

現時点ではなし（spec only）。S3 で `core/point_ledger.py`（planned）を作成予定。

## Related Worklog

- `docs/worklog/2026-05-22-spec-expand-session-ledger-scope.md`

## Related ADRs

- `docs/adr/0003-expand-domain-from-hand-logging-to-session-ledger-and-store-settlement.md`

## Related Commits

- 本 issue と同じコミット（spec expansion phase）

## Notes

関連する未確定事項（本 issue では扱わないが、別 issue を起こす候補）:

- "cash-out" という語の現仕様での意味（point 換金 vs cash 引き出し vs settlement 完了）が曖昧。
- hand logger と ledger app の参照同期方式（pull / push / event bus）の選定。
- session 中間集計の確定タイミングと UI 上の確定／途中値の区別表現。

これらは S2〜S5 着手前に追加 issue として起こす。
