# Worklog: RFID USB-CDC serial transport（ESP32-S3 USB 直結対応）

## Date

2026-06-01

## Scope / Task

ESP32-S3 を USB ケーブルで PC に直結する運用が確定したため、USB-CDC（仮想シリアル）を
読む新 transport `transport="serial"` を実装する（ADR-0007）。http / pcsc と併存させ、
既定 transport を serial に変更する。

## Goal

- ESP32-S3 が送る改行区切り JSON を USB-CDC 経由で受信し `RFIDEvent` を投入できる。
- 受信ペイロード契約（`reader_id`/`tag_id`/`timestamp`, ADR-0006）は不変のまま、配送手段
  だけを WiFi → USB シリアルに変える。
- 受信ロジックを transport 間で重複させず共通化し、HTTP の既存挙動は不変に保つ。
- code / tests / docs を一体で更新する。

## Changed Files

- `rfid/serial_receiver.py` — 新規。`RFIDSerialReceiver`（pyserial 遅延 import、改行区切り
  JSON 読み取り、オープン失敗/切断時の自動再接続、非 JSON 行スキップ、status プロパティ）。
- `rfid/event_builder.py` — 新規。`build_rfid_event`（payload → RFIDEvent 変換）と
  `_parse_timestamp` を transport 共通ロジックとして集約。
- `rfid/http_receiver.py` — `_handle_post` を `build_rfid_event` 呼び出しにリファクタ
  （挙動不変）。`_parse_timestamp` は event_builder から re-export（後方互換）。
- `main.py` — CLI/GUI 両経路に `transport=="serial"` 分岐を追加。GUI status hookup を
  `("http", "serial")` に拡張。
- `config_default.json` — 既定 `transport` を `http` → `serial` に変更。`serial_port` /
  `baudrate` / `reconnect_interval_ms` と説明コメントを追加（http/pcsc 設定は併存維持）。
- `requirements.txt` — `pyserial>=3.5` を optional（serial 時のみ）として追記。
- `tests/test_rfid_serial.py` — 新規。serial 受信の正常系/堅牢性/共通ヘルパ契約テスト。
- `CLAUDE.md` — ディレクトリ構成 / スレッド表 / 技術スタック / 実装状況 / エラーハンドリング
  方針を 3 transport + serial 既定に整合。
- `docs/adr/0007-...md` — 新規 ADR。`docs/adr/0006-...md` に ADR-0007 への back-link。
- `docs/issues/0007-...md` — USB-CDC を Resolved に、dual-support 回帰テストを紐付け。
- `docs/decision-log.md` / `CHANGELOG.md` — 索引・ユーザー可視変更を追記。

## Expected Behavior

- `transport="serial"`: USB-CDC ポートを開き、`{reader_id, tag_id, timestamp}` の改行区切り
  JSON を 1 行ずつ → `RFIDEvent` を rfid_queue へ。未知 reader_id は enqueue しない、
  未登録 tag は `card=""` で enqueue（HTTP と同挙動）。
- 非 JSON 行（ブートログ等）・空行はスキップしクラッシュしない。
- オープン失敗・切断時は `reconnect_interval_ms` で自動再接続。
- ISO14443A 4/7 バイト・ISO15693 8 バイト UID の双方が正規化される。
- HTTP transport の挙動は共通ヘルパ抽出後も完全不変。

## Implemented Behavior

期待どおり実装。`RFIDSerialReceiver` は `serial_factory` 注入で pyserial 非依存にテスト可能。
HTTP は `build_rfid_event` 呼び出しに置換し、既存テストが全 pass（挙動不変を確認）。

## Test Results

- 事前に `pip install pytest numpy`（本環境に未導入だったため）。
- `pytest tests/test_rfid_serial.py tests/test_rfid_http.py tests/test_rfid.py -q` → **59 passed**。
  - 新規 serial: 正常系 4 / 堅牢性 5（非 JSON skip・未知 reader・未登録 tag・ISO15693 8 byte・
    再接続）/ 共通ヘルパ 2。
  - 既存 http / rfid: リファクタ後も全 pass（回帰なし）。
- `python -m py_compile`（全変更ファイル）→ OK。

## Mismatches Found During Testing

None observed. 期待挙動と実装結果の不整合は検出されなかった（新規 issue 起票は不要）。

## Fixes Applied

- なし（mismatch なし）。リファクタ時に http_receiver の旧 `_parse_timestamp` 定義を削除し、
  event_builder からの re-export に一本化（重複解消。テストの import 経路は維持）。

## Remaining Gaps / Out-of-Scope

- [ ] GUI ステータスラベル（`gui/dashboard.py`）は `bind_port` 前提で、serial ではポート名が
      出ない（events 件数は表示、クラッシュなし）。cosmetic 改善は別タスク（ADR-0007 follow-up）。
- [ ] 実機 ESP32-S3 + PN5180 での end-to-end 通電確認（ハードウェア入手後）。
- [ ] タグ規格（ISO14443A / ISO15693）確定・UID エンコード順・クロストーク対策（ISSUE-0007 Open）。

## Related ADRs

- `docs/adr/0007-rfid-usb-cdc-serial-transport.md` — 本タスクの主判断
- `docs/adr/0006-rfid-hardware-migration-pn5180-esp32s3.md` — 上流（contract 不変）

## Related Issues

- `docs/issues/0007-rfid-pn5180-esp32s3-open-questions.md` — USB-CDC を Resolved 化

## Related Commits

- `<commit-sha>` — RFID USB-CDC serial transport
