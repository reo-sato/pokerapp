# 2026-09-10 — CHANGELOG ↔ 実装の全量整合監査 + 指摘修正

## Goal

CHANGELOG.md 全 77 セクション（1,237 行、プロジェクト全史）の記載が実装と整合しているかを
全量検証し（ユーザー依頼・工数無制限）、検出した指摘を修正して verify-v1 へマージする。

## 監査方法

- 11 の検証エージェントがサブシステム別にセクションを分担し、記載クレームを 1 件ずつ
  コード読解・個別テスト実行・`git log -S`/`git show`（歴史クレームの裏取り）で突き合わせ。
- 「軽微/不整合」とされた指摘は全件メインがコード・git で再確認（偽陽性除去）。
- 定量クレームは直接実行で確認: pytest 836 passed / skip 0、較正 P1〜P9 全 PASS、
  golden fixtures 13、mobile 30/30 + tsc green、staff 38/38 + tsc green、Playwright E2E 8 件。
- 判定基準: CHANGELOG は履歴文書のため「当時真であり、後続エントリで変更が文書化されている」
  ものは整合。歴史スナップショット値（テスト数 311→…→836 等）は増分の積み上げが現在値に
  収束することを確認。

## 監査結果

**不整合（機能・挙動・数値の食い違い）: 0 件 / 軽微: 10 件 + 付随 2 件**（整合 67 セクション）。
「実装されていない機能を実装済みと書いている」類の記載は無し。詳細はセッション報告書
（changelog-audit-2026-08-19.md, M-1〜M-10）。

## 修正内容（本 worklog のコミット）

### コード/設定（実装を記載に揃える）
- `staff/playwright.config.ts` — **M-2**: top-level `use` の viewport/hasTouch が project 側
  `...devices["Desktop Chrome"]` にキー単位で上書きされ実効していなかった（実効 1280×720 /
  touch なし）。device spread の後に viewport 1180×820 + hasTouch:true を再指定。
- `mobile/src/screens/OrderScreen.tsx` — **M-5**: メニュー正常表示にも `ReloadLink` を追加
  （「全画面に ↻ 再読込」の記載どおりに。従来はエラー時再試行 + 注文状況の更新のみ）。
- `tests/test_ledger_view_gui.py` — **M-6/M-7**: B1 コミット（cad581a）で `TestCloseSession`
  配下に紛れていた支払系 5 テスト（set_payment ×2 / record_payment ×3 + helper）を本来の
  `TestSettlement` へ移動（挙動・件数不変、31 passed）。

### docs drift（記載を実装に揃える）
- `README.md`（2 箇所）+ `pyproject.toml` description — **M-1**: 旧「ESP32 + PN532」を
  canonical「PN5180 + ESP32-S3（USB CCID / PC/SC）」（ADR-0015/0034）に更新。
- `core/poker_engine.py` docstring — **M-10**: 「default は legacy / pokerkit は default-off の
  preview」→ Phase G 以降の実態（既定 pokerkit / legacy = rollback）に更新。
- `docs/contracts/repository-interfaces.md` — 付随 1: 非採用 `plan_payment` の stale 言及を
  ADR-0016 の実態（不足分 cash は呼び出し側が明示）に修正。
- `tests/test_contracts.py` コメント — 付随 2: 「S2/S3 draft 未 freeze」の陳腐化記述を
  ADR-0019 で 1.0 freeze 済み + 現行 version（player 1.2 / hand・action 1.1 等）に更新。

### CHANGELOG 自体の誤記訂正（最小編集）
- **M-9**: ADR-0039 エントリの表記崩れ「`main.py --`（GUI）」→「`main.py`（GUI モード `run_gui`）」。
- **M-4**: ADR-0013 エントリの契約 draft ファイル名。verify-v1 統合マージ（d0fb96c）で
  `ledger-points.md` → `ledger-overview.md` に遡及書き換えされていたのを、当時の実態 +
  「統合時に統合・削除」の注記に復元（統合エントリとの自己矛盾を解消）。
- **M-8**: Phase H エントリの requirements-dev 記述に fastapi/httpx（M1 API テスト用）+ ruff
  （CI lint 用）の後続追加を追記。
- **M-3**: F3a エントリに `_hand_needs_review` の finalize 時リセット（commit 50101af に
  含まれていたが未記載だった堅牢化 + テスト 5 件目）を「監査で補記」と明示して追記。

## Test results

- `pytest tests/ --ignore=tests/test_vision.py`: 836 passed / skip 0（テスト移動で件数不変）
- `ruff check .`: pass
- mobile: 30/30 + tsc green / staff: 38/38 + tsc green
- staff Playwright E2E: 8 passed（viewport 修正後に実行して green を確認）

## Remaining gaps

- なし（監査指摘は全消化。M-2 の実機 iPad タッチ QA は従来どおり Phase H の実機タスク）。
