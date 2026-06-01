# ADR-0007: ESP32-S3 USB 直結のため USB-CDC serial を RFID の主 transport に採用する

## Status

Accepted

## Date

2026-06-01

## Context

ADR-0006 で RFID ハードウェアを PN5180 + ESP32-S3 に移行し、その時点では受信経路として
WiFi 経由の HTTP transport（`RFIDHTTPReceiver`）を既定としていた。ADR-0006 の
Alternative B では「ESP32-S3 のネイティブ USB を活かす USB-CDC serial transport」を
deferred（設計候補）として ISSUE-0007 に記録していた。

その後の運用方針確定により、**ESP32-S3 を USB ケーブルで PC に直結する**ことになった。
ESP32-S3 はネイティブ USB-OTG を持つため、USB 直結時は PC からは **USB-CDC（仮想シリアル
ポート, 例: `/dev/ttyACM0` / `COM3`）** として見える。これは既存 2 transport のどちらでも
扱えない:

- `http`（WiFi）: USB 直結では使わない。
- `pcsc`（pyscard）: ACR122U 等の **完成品 USB NFC リーダー**を ISO7816 APDU で叩く経路。
  ESP32-S3 を挿しても PC/SC デバイスにはならないため使えない。

したがって新しい `serial` transport が必要になった。受信ペイロード契約（`reader_id` /
`tag_id` / `timestamp`）は ADR-0006 のとおり transport 非依存であり、配送手段だけが
WiFi → USB シリアルに変わる。

関連: ADR-0006、ISSUE-0007、`CLAUDE.md`（アーキテクチャ / 技術スタック）。

## Decision

USB-CDC serial を新しい RFID transport（`transport="serial"`）として追加し、**既定の
transport を `serial` に変更する**。`http`（WiFi）/ `pcsc`（USB NFC リーダー）は引き続き
併存させ、3 transport を config の `transport` で選択可能とする。

- 受信スレッド `rfid/serial_receiver.py::RFIDSerialReceiver` を新設する。ESP32-S3 が送る
  **改行区切り JSON**（1 行 1 イベント）を読み、`RFIDEvent` を rfid_queue に投入する。
- 受信ペイロード → `RFIDEvent` 変換は transport 間で重複させず、共通ヘルパ
  `rfid/event_builder.py::build_rfid_event` に集約する。`RFIDHTTPReceiver` もこれを呼ぶ
  ようリファクタし、validation・card lookup・tag 正規化の **単一 source** とする。
- `pyserial` は遅延インポートし、`transport="serial"` 時のみ必須とする（pyscard と同方針）。
- ESP32-S3 の再起動・USB 抜き差しに耐えるよう、オープン失敗・読み取りエラー時は
  `reconnect_interval_ms` 間隔で自動再接続する。

## Alternatives Considered

- **Alternative A — WiFi HTTP を主のまま維持し USB は採用しない**
  - Pros: 追加実装ゼロ。
  - Cons: 運用方針（USB 直結）と矛盾する。卓上固定で WiFi の不安定さ・遅延を抱える。
  - Why rejected: 運用が USB 直結に確定したため。
- **Alternative B — `pcsc`（pyscard）経路を流用する**
  - Pros: 既存コードを使える。
  - Cons: pcsc は PC/SC リーダー（ACR122U 等）専用。ESP32-S3 の USB-CDC は PC/SC デバイス
    ではないため原理的に扱えない。
  - Why rejected: 技術的に不可能。
- **Alternative C — serial 受信ロジックを RFIDHTTPReceiver と独立に複製する**
  - Pros: http に一切手を入れない。
  - Cons: tag 正規化・reader 検証・card lookup・timestamp 解析が 2 箇所に重複し、
    将来の契約変更で drift する。core を単一 source とする方針（CLAUDE.md）に反する。
  - Why rejected: `build_rfid_event` 抽出で DRY 化し、http の挙動は不変に保った。

## Consequences

- Positive: USB 直結運用に対応。transport を増やしても受信ペイロード契約と下流
  （`RFIDEvent` 以降）は不変。validation が共通ヘルパに一元化された。
- Negative / trade-offs: `transport="serial"` 時は `pyserial` が追加依存になる。
  GUI のステータスラベル（`gui/dashboard.py`）は `bind_port` 前提のため、serial では
  ポート名が表示されない（events 件数は表示される）。挙動は壊れない（`.get` 既定値）。
- Neutral / new constraints: 改行区切り JSON をファームウェア契約として固定する。
  PN5180 高 RF 出力に伴うクロストークはファーム/アンテナ側責務として残る（ISSUE-0007）。

## Validation / Follow-up

- [x] `RFIDSerialReceiver` の正常系 / 非 JSON スキップ / 未知 reader / 未登録 tag /
      ISO15693 8 バイト UID / 自動再接続をテスト（`tests/test_rfid_serial.py`）。
- [x] `RFIDHTTPReceiver` の既存テストが共通ヘルパ抽出後も全 pass（挙動不変を確認）。
- [ ] GUI ステータスラベルを serial 用に整える（cosmetic, 別タスク）。
- [ ] 実機 ESP32-S3 + PN5180 での end-to-end 通電確認（ハードウェア入手後）。

## Related Files

- `rfid/serial_receiver.py` — 新規 serial 受信スレッド
- `rfid/event_builder.py` — 新規 transport 共通ヘルパ
- `rfid/http_receiver.py` — 共通ヘルパ呼び出しへリファクタ（挙動不変）
- `main.py` — `transport="serial"` 分岐 + GUI status hookup
- `config_default.json` — 既定 transport を serial に変更 + serial 設定追加

## Related Tests

- `tests/test_rfid_serial.py::TestSerialReceiverSuccess`
- `tests/test_rfid_serial.py::TestSerialReceiverRobustness`
- `tests/test_rfid_serial.py::TestBuildRfidEvent`
- `tests/test_rfid_http.py`（リファクタ後も全 pass）

## Related Commits

- `<commit-sha>` — RFID USB-CDC serial transport

## Supersedes / Superseded by

- Supersedes: — （ADR-0006 の Alternative B〔deferred〕を Accepted 化。ADR-0006 自体は有効）
- Superseded by: —
