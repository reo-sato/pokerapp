# Issue 0012: Session / Seating Viewer の拡張（WS2-α の外に出した論点）

## Date

2026-06-04

## Status

Open（WS2-α で read-only 最小版を実装。以下は将来拡張として保留）

## Severity / Priority

- Severity: Low（α viewer で「中身を覗く」最低限は満たした。拡張は ledger/points 等と並行で検討）
- Priority: P3

## Area

desktop GUI（WS2）/ session・seating inspection

## Context

WS2-α で read-only の Session / Seating Viewer（`gui/session_viewer.py` の
`SessionViewerWindow`）を追加した。session 一覧 → 選択 → 概要 / current seating /
hand 別 seat assignments を表示し、`player_id` を `display_name` に解決する。手動 Refresh のみ。

α として意図的に最小構成にしたため、以下を **scope 外**として本 issue に退避する。

## まだ Open な拡張候補

1. **filter / search / sort**: session 一覧の status / 日付での絞り込み、player 名での検索、
   hand assignments の seat / hand 順ソート。現状は作成順一覧 + 全件表示のみ。
2. **live auto-refresh**: hand logger 稼働中に viewer をポーリング更新（現状は手動 Refresh）。
   スレッド安全性（IntegrationThread と同一 repository を読む）の設計が必要。
3. **hand assignments の集約表示**: 現状は (hand_id, seat_no) ごとに 1 行のフラット表示。
   hand 単位のグルーピング / seat map のマトリクス表示など可読性向上。
4. **export**: CSV / JSON への書き出し（read-only の延長）。
5. **session close 等の操作**: 本 viewer は read-only 厳守のため持たない。編集系を別画面で
   持つか否かは別途判断（ledger/settlement UI と合わせて WS2 本体で検討）。
6. **mobile / web viewer**: 同じ contract に対する別 front-end（WS3）。未着手。
7. **ledger / points / settlement との統合 view**: S3 / S4 実装後に session を軸とした
   集計ビューを重ねる余地。

## 関連

- 実装: `gui/session_viewer.py`、`core/session_repository.py`（`list_hand_ids` 追加）、
  `main.py`（`--sessions-viewer`）、`gui/dashboard.py`（Session Viewer ボタン）。
- worklog: `docs/worklog/2026-06-04-session-seating-viewer-ws2-alpha.md`
- 関連 ADR: ADR-0006 / ADR-0007（S2 contract / 永続形）、ADR-0008（hand 接続）。
- 関連 issue: ISSUE-0006（seat 選択 UX）、ISSUE-0007（legacy log 取り込み）。

## Notes

read-only viewer であることは設計上の約束（CLAUDE.md § Session / Seating Viewer）。
編集機能を本 viewer に足さないこと。拡張時も repository を source of truth とし、
GUI に business ルールを複製しない。
