# Issue 0020: 店舗用 staff iPad アプリ — 設計の open question / risk register

## Date

2026-06-16

## Status

Open

## Severity / Priority

- Severity: Medium（設計フェーズの未決事項。実装着手前に解消したい）
- Priority: P2

## Area

gui / staff app / viewer API / integration（ADR-0037 / ADR-0038）

## Expected Behavior

ADR-0037（staff iPad アプリ）/ ADR-0038（staff API 拡張）に沿って、店舗操作を 1 つのタッチアプリで
完結できる。特に **ハンドロガー遠隔制御**・**認可運用**・**並行/オフライン**の各点が、実装着手前に
方針確定している状態。

## Actual Behavior

設計フェーズ（コードなし）であり、以下が **未確定（open）**。本 issue を risk register として残す。

## Reproduction

該当なし（設計上の未決事項の列挙）。

## Open Questions（要決着）

1. **hand logger 遠隔制御のプロセス境界（最重要, ADR-0038 §C）** — **✅ Resolved（ADR-0039）**:
   選択肢 (a) **append-only control queue**（`logs/{session_id}.control.jsonl` を hand logger が tail）を
   採用・実装。staff API が append、hand logger プロセスの `ControlConsumerThread` が末尾シーク + 
   `command_id` 重複排除で新規のみを `AudioEvent` に翻訳し IntegrationThread に渡す（状態変更経路を
   増やさない, ISSUE-0012）。既定 off（`hand_control.enabled`）+ GUI + `session_layer.enabled` が前提。
   録音は PC 維持（ADR-0037 §4）。`core/control_queue.py` / `integration/control_consumer.py` /
   `POST /api/staff/sessions/{sid}/control` / staff app ハンドタブ。**残**: ハンド履歴 read（staff 用
   hands-list endpoint）と実機での反映遅延・死活の確認。

2. **session ⇔ hand logger の結線**: staff app から作った session（ADR-0038 §B の `POST
   /api/staff/sessions`）を、録音中の hand logger が **どの session_id で書くか**。現状 hand logger は
   `session_layer.enabled` 時に自プロセスで session を採番する（`main.py:run_gui`）。iPad 作成 session と
   突き合わせる運用（PC 側で「既存 session を選んで録音開始」する UI / フラグ）が必要かを決める。

3. **認可運用（shared token の配布・回転）**: staff token は端末に保存（ADR-0037 §3）。複数 iPad
   への配布、漏洩時の失効、token 回転手順をどうするか（ADR-0021 は per-staff 監査なしを許容）。
   LAN 限定前提は維持。

4. **並行 / オフライン耐性**: 複数 iPad が同一 session を同時操作した場合の UX（reload-on-read +
   単一書き手 = ADR-0021 で data race は無いが、画面の stale 表示の更新方針 = polling / 手動 refresh /
   将来 push）。iPad の一時オフライン時の挙動（write 失敗の再試行 / キュー）。

5. **desktop GUI と staff app の責務分界の最終形**: 当面並存（ADR-0037）だが、将来 desktop を
   staff app に寄せて縮退させるか、PC は録音専用 + iPad は操作専用に役割固定するか。

6. **player 自身の order との整合**: 注文確定（confirm）の単価 prefill は menu master（ADR-0018）。
   iPad 側でも menu を read して prefill する（`GET /api/menu`）。closed session への確定は
   `session_closed`（409）で弾かれる挙動を UI でどう見せるか。

## Root Cause

新規 front-end（iPad）と既存の **プロセス構成（録音は PC 常駐 / 会計 write は `--ledger`）** の
境界をまたぐため、front-end 単独では決められない運用・所有・並行性の判断が残る。

## Fix

- Q1（hand logger 制御）: control queue 方式を spike → 別 worklog / 必要なら追補 ADR で確定。
- Q2（session 結線）: ADR-0038 §B 実装時に hand logger 側の「既存 session 選択」運用を併せて設計。
- Q3–Q5: staff app 実装の運用ドキュメント（`staff/README.md`）で確定。
- Q6: staff app 実装時に UI メッセージへ error code をマッピング（`mobile/` の AuthScreen に倣う）。

## Regression Test

- 設計フェーズのため該当なし。各 Q 決着時に対応テストを追加（hand logger 制御は決定的 replay/queue
  テスト、API は `tests/test_viewer_api_staff.py`）。

## Affected Files

- `docs/adr/0037-staff-ipad-app-touch-frontend.md`
- `docs/adr/0038-staff-api-session-seat-handlogger-expansion.md`
- `staff/`（新規, planned）/ `api/server.py` / `main.py` / `integration/engine.py`

## Related Worklog

- `docs/worklog/2026-06-16-staff-ipad-ux-design.md`

## Related ADRs

- `docs/adr/0037-staff-ipad-app-touch-frontend.md`
- `docs/adr/0038-staff-api-session-seat-handlogger-expansion.md`
- 関連: ADR-0008（hand logger × session）/ ADR-0018（in-process API）/ ADR-0021（staff token）

## Related Commits

- 本 issue（設計のみ）と同じ commit

## Notes

ISSUE-0009（actor/silent-fold）/ ISSUE-0013（session viewer data source）と同様、hand logger の
プロセス所有境界に触れる open question。実装は会計/注文（API 済）→ session/座席（ADR-0038 §A/B）→
hand logger 制御（§C, 本 issue Q1 決着後）の順を推奨。
