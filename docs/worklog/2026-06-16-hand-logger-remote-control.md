# Worklog: hand logger 遠隔制御 + staff アプリ ハンドタブ（WS4 §C, ADR-0039）

## Date

2026-06-16

## Scope / Task

ADR-0038 §C（hand logger 遠隔制御）を実装する。プロセス境界（録音 = 別プロセス常駐）を越えるため、
ISSUE-0020 Q1 の第一候補 **append-only control-command queue** を採用（ADR-0039）。staff アプリに
ハンドタブを足し、新ハンド/ウィナー/リバイを iPad から送れるようにする。録音は PC 常駐のまま。

## Goal

- staff API が `logs/{session_id}.control.jsonl` にコマンドを append、hand logger プロセスがそれを tail
  して `AudioEvent` に翻訳し IntegrationThread に渡す（新しい状態変更経路を作らない, ISSUE-0012）。
- 既定 off（`hand_control.enabled`）+ GUI + `session_layer.enabled` のときのみ有効 → 既定で挙動不変。
- Python 全テスト + staff アプリ typecheck/test/E2E build が green。

## Changed Files

- `core/control_queue.py` — `ControlCommand` / `ControlCommandLog`（append / read_from(offset) /
  end_offset）+ `command_to_audio_event`（new_hand/winner/rebuy → AudioEvent）。新規。
- `integration/control_consumer.py` — `ControlConsumerThread`（末尾シーク開始・command_id 重複排除・
  poll で新規のみ audio_queue へ）。新規。
- `config_default.json` — `hand_control`（enabled 既定 false / poll_interval_ms）。
- `main.py` — `run_gui` に consumer 結線（hand_control.enabled + session_layer.enabled、cleanup で join）。
- `api/server.py` — `POST /api/staff/sessions/{sid}/control` + `_StaffControlBody` + `invalid_control`。
- `api/client.py` — `ViewerApiClient.send_control`。
- `tests/test_control_queue.py`（新規）/ `tests/test_viewer_api_staff_lifecycle.py`（control 追加）。
- `staff/src/api/types.ts`（`HandControlInput` / `ControlCommand`）/ `repository.ts`（`sendControl`）/
  `mockRepository.ts` / `httpRepository.ts`（実装）。
- `staff/src/screens/HandTab.tsx`（新規）/ `TableViewScreen.tsx`（ハンドタブ統合）。
- `staff/src/api/mockRepository.test.ts`（sendControl）/ `staff/e2e/staff.spec.ts`（ハンドタブ送信）。
- docs: `docs/adr/0037-...md`（新規）/ `docs/issues/0020-...md`（Q1 Resolved）/ `decision-log.md` /
  `docs/contracts/viewer-api.md`（§C + invalid_control）/ `CLAUDE.md` / `CHANGELOG.md`。

## Expected Behavior

- staff が iPad のハンドタブから 新ハンド / ウィナー(席) / リバイ(席+額) を送ると、control queue に
  1 行 append され、hand logger（consumer 起動時）が新規分だけを AudioEvent にして反映する。
- consumer は起動時に末尾シーク（過去コマンドを再実行しない）+ command_id 重複排除。
- 既定 off では consumer を起動しない（挙動不変）。

## Implemented Behavior

- 上記どおり。control の type/args 不正は API/mock とも `invalid_control`(400)。録音は PC 常駐のまま、
  iPad は append のみ（fire-and-forget、ack 表示 + 画面内送信履歴）。

## Test Results

- `python -m pytest tests/ -q --ignore=tests/test_vision.py` — **634 passed**（回帰なし。
  `test_control_queue.py` + staff control テスト含む）。
- `cd staff && npm run typecheck` — エラーなし。`npm test` — 13 passed。
- `npm run export:web` — green。`npx playwright test --list` — 6 tests 検出（ハンドタブ送信含む）。
  browser 実行は要ネットワーク（本環境では未実行、別環境で `npm run e2e`）。

## Mismatches Found During Testing

- None observed（実装・テストとも想定どおり）。

## Fixes Applied

- なし。

## Remaining Gaps / Out-of-Scope

- [ ] ハンド履歴の staff read（hands-list endpoint）。ハンドタブ v1 は制御送信のみ（履歴は未表示）。
- [ ] 実機での反映遅延（poll 既定 200ms）/ hand logger 死活の確認は実機 QA。
- [ ] CLI モード / 非 session レイヤは非対象（session_id が sessions.json に無く staff が卓を選べない）。
- [ ] E2E は mock（送信 ack）まで。実 hand logger との結合 E2E は実環境タスク。

## Related ADRs

- `docs/adr/0039-hand-logger-remote-control-command-queue.md`（本実装）
- `docs/adr/0038-staff-api-session-seat-handlogger-expansion.md`（§C を ADR-0039 へ）

## Related Issues

- `docs/issues/0020-staff-ipad-app-open-questions.md`（Q1 Resolved）

## Related Commits

- 本 worklog と同じ commit
