# Issue 0022: Windows の汎用 CCID ドライバは 1 slot しか公開しない（multi-slot が見えない）

## Date

2026-09-10

## Status

Fixed（設計変更 = ADR-0041 / 契約 v1.2。**実機 2026-09-11（firmware `0e93de4`, 10 台 ready）で host 側を
通しで確認**: `probe_pcsc list` = reader 1 件 + `physical readers: 11` + 11 件 matched / `check` = 11 行 PASS
（未通電 index は `6A81` で PASS）/ `watch` = 席 8 台 × 2 枚 + board で 22 タッチ、`seat N [rK]` の対応が
config どおり。残 1 台はコネクタ #11（ch10）の未通電 = ISSUE-0023）

## Severity / Priority

- Severity: Blocker（本番 11 台構成が成立しない）
- Priority: P0

## Area

rfid / firmware ↔ host boundary（PC/SC canonical, 契約 `docs/contracts/rfid-usb-ccid.md`）

## Expected Behavior

契約 v1.0/1.1 §3（ADR-0034）: firmware は PN5180 1 台 = CCID **1 slot** として公開し、
**slot ごとに一意な reader_name** が PC/SC に現れる。本番は 11 slot（席 8 + board 3）で、
`config.rfid.pcsc_readers[].name` に 11 個の reader_name を書けば `RFIDThread` が 11 台を polling する。
段階検証として `CCID_SLOT_COUNT=2` なら `PokerRFID PN5180-CCID 0` と `… 1` の 2 個が見えるはず。

## Actual Behavior

`CCID_SLOT_COUNT=2`（CCID functional descriptor の `bMaxSlotIndex=1`、`bcdDevice` も更新）で
Windows に接続しても、PC/SC には **`PokerRFID PN5180-CCID 0` の 1 個しか現れない**。

- `python tools/probe_pcsc.py list` → 接続中の PC/SC reader: 1 件（`… 0` のみ）。
- config に `… 1` を書くと host は `Reader not found: 'PokerRFID PN5180-CCID 1'`（`PCSCBridge.connect`）。
- firmware 側は 2 slot ぶんの CCID メッセージを処理できる状態（slot 1 宛のコマンドが届かないだけ）。

## Reproduction

1. `firmware/esp32s3-pn5180-ccid` を `CCID_SLOT_COUNT=2` でビルドして書き込む（`bcdDevice` も変える）。
2. Windows PC に USB 接続し、デバイスマネージャーで `Microsoft Usbccid Smartcard Reader (WUDF)` を確認。
3. `python tools/probe_pcsc.py list` を実行。
4. → reader は 1 件だけ。`… 1` は現れない（`config.rfid.pcsc_readers` に書くと MISSING / connect 失敗）。

## Root Cause

**Microsoft の汎用 CCID class driver（usbccid）が 1 インターフェース 1 slot しかサポートしない**
（multi-slot reader の 2 番目以降の slot を列挙しない）。firmware / 記述子の不具合ではなく、
OS ドライバ側の既知の制限:

- SpringCard TechZone: *"Microsoft's generic CCID driver supports single-slot readers only and
  doesn't even show the other slots"*。
- Microsoft Q&A: SEC1210 ベースの dual-slot reader で 2 つ目の slot が pyscard から見えない同一報告。

回避策の「slot ごとに USB インターフェースを分ける（composite）」は、ESP32-S3 の USB device
controller の **endpoint が 6 本（双方向 5 + IN 1）** しかないため CCID を最大 5 個までしか置けず、
本番 11 台に届かない。

## Fix

**ADR-0041 / 契約 v1.2**: USB 上の CCID slot は **常に 1 つ**にし、**物理リーダーは Get UID
pseudo-APDU の P2 で選ぶ**。

- firmware: `FF CA 00 <k> 00` → リーダー k の UID（複数枚は 8B 連結）+ `90 00` / カード無し `6A 81` /
  範囲外 `6A 86`。`FF CA 00 FF 00` → 台数 `<N>` + `90 00`。`k=0` は v1.0/1.1 と同一。
- host: config `rfid.pcsc_readers[]` に `reader`（任意・既定 0）。11 件すべて同じ `name` +
  `reader` 0..10。一意性は `(name, reader)`。接続は reader 名ごとに 1 本を持続し、
  そこに N 個の Get UID を流す。
- 変更ファイル: `rfid/bridge.py` / `rfid/reader_thread.py` / `tools/probe_pcsc.py` /
  `tools/register_cards.py` / `config_default.json` / `docs/contracts/rfid-usb-ccid.md`（v1.2）。

## Regression Test

- `tests/test_rfid.py::TestPCSCBridgeReaderIndex::test_apdu_carries_reader_index_as_p2`
- `tests/test_rfid.py::TestPCSCBridgeReaderIndex::test_shared_connection_per_reader_name`
- `tests/test_rfid.py::TestPCSCBridgeReaderIndex::test_sw_6a86_returns_empty_and_warns_once`
- `tests/test_rfid.py::TestQueryReaderCount::test_returns_count_from_firmware`
- `tests/test_tools_probe_pcsc.py::TestLint::test_duplicate_name_is_allowed_when_reader_differs`
- `tests/test_tools_probe_pcsc.py::TestLint::test_shipped_default_config_passes_lint`
- `tests/test_tools_probe_pcsc.py::TestCommandsWithPyscardStubbed::test_list_warns_when_reader_index_exceeds_count`

## Affected Files

- `rfid/bridge.py` / `rfid/reader_thread.py`
- `tools/probe_pcsc.py` / `tools/register_cards.py`
- `config_default.json`
- `docs/contracts/rfid-usb-ccid.md`（v1.1 → v1.2）
- `firmware/esp32s3-pn5180-ccid/`（firmware 側: `CCID_SLOT_COUNT=1` 固定 + `PN5180_READER_COUNT`（既定 11）+
  `handle_apdu` の P2 選択 / `6A 86` / 台数問い合わせ。スタブ 128 構成 + simulator で確認、**実機未検証**）

## Related Worklog

- `docs/worklog/2026-09-10-rfid-reader-index-p2-host.md`（host 側）
- `docs/worklog/2026-09-10-pn5180-reader-index-p2-firmware.md`（firmware 側）

## Related ADRs

- `docs/adr/0041-physical-reader-index-via-get-uid-p2.md`（本 issue の Fix）
- `docs/adr/0040-ccid-virtual-card-always-present.md`（slot 常時 present。1 接続持続の前提）
- `docs/adr/0034-rfid-usb-ccid-firmware-host-contract-freeze.md`（v1.0 契約 = 置き換え対象の §3）

## Related Commits

- （本タスク）host + 契約 v1.2

## Notes

- ISSUE-0015（USB CCID firmware contract）/ ISSUE-0021（複数枚 anti-collision + poll 周期）の続き。
  ISSUE-0021 の「1 周のレイテンシ」は 11 台では **1 接続 × 11 APDU** の合計になるため、
  実機で poll 1 周を計測して `poll_interval_ms` を決めること。
- Linux/pcsc-lite は multi-slot を列挙できるが、本番 OS は Windows なので OS 分岐は作らない
  （契約 v1.2 §3 で v1.1 の slot 規約を廃止）。
