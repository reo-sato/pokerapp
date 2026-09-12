# Worklog: 店舗用 staff iPad アプリ UX の整理と設計（設計フェーズ・コードなし）

## Date

2026-06-16

## Scope / Task

店舗用 iPad の UX を整理・改修するにあたり、(1) プロジェクト全体像と店舗（スタッフ）向けの現状
UI/UX を棚卸しし、(2) 新規 staff iPad アプリの設計ドキュメント（ADR + 画面/遷移仕様 + 不足 API 設計 +
open question）を書き起こす。**コード変更なし**（設計先行）。

## Goal

- 現状の店舗アプリ（desktop GUI + staff API）の UI/UX を構造化して把握できる状態にする。
- 「新規 staff iPad アプリ」の方針・技術・画面/遷移・認可・所有境界・不足 API を ADR として確定し、
  実装着手の足場（DoD・risk register）を用意する。

## Changed Files

- `docs/adr/0037-staff-ipad-app-touch-frontend.md` — 新規。staff iPad アプリのアーキテクチャ
  （別アプリ / Expo・RN / staff token / 録音は PC・iPad は操作 / 卓単位タブ統合 / 画面・遷移仕様 /
  contract-first repository 注入）。
- `docs/adr/0038-staff-api-session-seat-handlogger-expansion.md` — 新規。不足 staff API の設計
  （A: 会計の reversal/grant、B: session/座席/player ライフサイクル、C: hand logger 遠隔制御=後続）。
- `docs/issues/0020-staff-ipad-app-open-questions.md` — 新規。open question / risk register
  （hand logger プロセス境界・session 結線・token 運用・並行/オフライン・責務分界）。
- `docs/decision-log.md` — ADR-0037/0038 を ADR Index に、ISSUE-0020 を Issue Index に追記。
- `CLAUDE.md` — Future Scope に WS4（staff iPad app）/ Phase T を planned で追加。
- `CHANGELOG.md` — Unreleased に設計ドキュメント追加を記録。

## Expected Behavior

- 現状整理: 店舗操作 = desktop customtkinter（5 画面・別プロセス起動）+ staff write API
  （`/api/staff/...`）。player 向け `mobile/` とは関心・認可が別。
- 設計: 店舗操作を 1 つの Expo/RN staff アプリ（iPad/web）に統合。会計/注文は staff API が揃っており
  最短実装可、session/座席は API 追加（ADR-0038 §B）が前提、hand logger 制御は別プロセスのため後続。

## Implemented Behavior

- 上記 6 ファイルを作成/更新。ADR-0037/0038 は Status=Proposed（設計のみ）。ISSUE-0020 は Open。
- staff API カバレッジを 4 優先機能に突き合わせた結果を ADR-0038 Context に表で明文化
  （会計=ほぼ揃う / 注文=揃う / session・座席=大きく欠落 / hand logger=欠落かつプロセス境界）。

## Test Results

- 設計フェーズ・コードなしのためテスト追加なし。既存テストへの影響なし（コード不変）。
- ドキュメント整合は目視確認（ADR/issue/worklog/decision-log/CLAUDE.md/CHANGELOG の相互リンク）。

## Mismatches Found During Testing

None observed（コード変更なし）。

## Fixes Applied

なし。

## Remaining Gaps / Out-of-Scope

- [ ] ADR-0037/0038 の実装（`staff/` scaffold、staff API 追加）は本タスク scope 外（設計のみ）。
- [ ] hand logger 遠隔制御（ADR-0038 §C / ISSUE-0020 Q1）の方式確定（spike が必要）。
- [ ] `staff/` 実装着手時に `docs/contracts/viewer-api.md` の staff write 節へ A/B を追記。
- [ ] 方針決定: 実装順は 会計/注文（API 済）→ session/座席（§A/B）→ hand logger 制御（§C）を推奨。

## Related ADRs

- `docs/adr/0037-staff-ipad-app-touch-frontend.md` — staff iPad アプリのアーキテクチャ
- `docs/adr/0038-staff-api-session-seat-handlogger-expansion.md` — 不足 staff API 設計
- 関連: ADR-0017 / 0018 / 0020 / 0021 / 0026 / 0008 / 0030

## Related Issues

- `docs/issues/0020-staff-ipad-app-open-questions.md` — open question / risk register

## Related Commits

- 本 worklog と同じ commit
