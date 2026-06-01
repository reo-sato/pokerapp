# Issue 0006: PN5180 + ESP32-S3 firmware ↔ Python の HTTP API 契約固定

## Date

2026-06-01

## Status

**Superseded by ISSUE-0007**（2026-06-01）。本 issue は ADR-0007（Superseded）の前提
（HTTP transport が canonical）で起こした。ADR-0008 で **PC/SC 経路が canonical** に方針変更
されたため、firmware ↔ host の契約対象が HTTP API から USB CCID / PC/SC に移動。後続の
open question は ISSUE-0007（USB CCID firmware 契約）で追跡する。本 issue は history として残す。

## Severity / Priority

- Severity: Medium（hardware 移行が進むと顕在化。Python 側の receiver 改修が firmware 仕様に
  追従する必要）
- Priority: P2

## Area

rfid / firmware boundary / contract

## Expected Behavior

ADR-0007 の方針に従い、ESP32-S3 firmware と Python `RFIDHTTPReceiver` の間の **HTTP API 契約が
固定** されている:

- `POST /rfid`: body は `{"reader_id": "...", "tag_id": "...", "timestamp": "..."}`。
- `reader_id` 命名: `seat_1..9` / `board_1..5`。
- `tag_id` 表記: colon-separated upper-hex（例 `"04:AB:CD:EF:12:34:56:78"`）。UID 長は
  ISO 15693 で 8B、過去資産で 4B/7B も読み込めるよう受信側で許容。
- `timestamp`: ISO 8601 文字列。
- `GET /status`: 各 reader の最終受信時刻 / 接続状態を返す。
- error response の形（HTTP code / body）が決まっている。
- heartbeat / keepalive の有無と間隔。

## Actual Behavior

- `POST /rfid` の JSON 契約は既存 `rfid/http_receiver.py` の docstring に明文化済み（reader_id /
  tag_id / timestamp）。
- ただし以下は **未確定**:
  1. PN5180 firmware が生成する **`tag_id` の正確な書式**（colon-hex vs 連結 hex、大文字小文字）。
  2. **UID 長 8B（ISO 15693）対応**を `normalize_tag_id` / regex が許容するかの実機検証。
  3. **error response** の body 形（現在は `BaseHTTPRequestHandler` 既定）。
  4. **heartbeat / status エンドポイント** の詳細（payload schema、間隔）。
  5. ESP32-S3 firmware の **WiFi 切断時挙動**（再接続 / バッファリング / 失われるイベントの扱い）。

## Reproduction

仕様レビュー（バグではなく firmware 仕様確定前の open question）:

1. `rfid/http_receiver.py` の docstring を読む。
2. `rfid/card_master.py` の `normalize_tag_id` を読む。8B UID に対する単体テストが無い。
3. PN5180 firmware は本 repo 外（別プロジェクト）。

## Root Cause

ハードウェア仕様変更（ADR-0007）で reader IC が世代交代したが、firmware と Python の **API 契約**
（JSON schema 相当）が文章化されていない。CLAUDE.md / docs/contracts/ の流儀に倣えば、本来は
RFID HTTP 契約も `docs/contracts/` に schema として置くべき（将来 task）。

## Fix

未対応。S2 着手後・S3 前のどこかで以下を順次クローズ:

- firmware 側のサンプル payload を本 issue に貼り、`tag_id` の確定書式を固定する。
- `tests/test_rfid_http.py` に 15693（8B）UID fixture を追加し、`normalize_tag_id` 互換を回帰固定。
- `docs/contracts/` に `rfid_event.schema.json`（POST /rfid body 用）を additive で追加（将来）。
- error / heartbeat / 切断時挙動を CLAUDE.md § エラーハンドリング方針に追記。

## Regression Test

未実装。Fix の一部として以下を追加:

- `tests/test_rfid_http.py`: 8B UID fixture の POST → `RFIDEvent` 正常化。
- `tests/test_rfid.py`（card_master）: `normalize_tag_id("04AB...8byte")` の roundtrip。

## Affected Files

- `rfid/http_receiver.py`
- `rfid/card_master.py`
- `tests/test_rfid_http.py` / `tests/test_rfid.py`
- 将来: `docs/contracts/schemas/rfid_event.schema.json`（additive）

## Related Worklog

- `docs/worklog/2026-06-01-pn5180-esp32s3-rfid-migration-planning.md`

## Related ADRs

- `docs/adr/0007-migrate-rfid-to-pn5180-esp32s3-and-canonical-http-transport.md`

## Related Commits

- 本 issue と同じコミット（RFID hardware migration planning）。

## Notes

PCSC 経路（`rfid/reader_thread.py`）の完全削除可否は本 issue の scope 外。必要なら
別 ADR / issue を起こす。
