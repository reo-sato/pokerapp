# ADR-0007: Migrate RFID hardware to PN5180 + ESP32-S3 and make HTTP the canonical transport

## Status

**Superseded by ADR-0008**（2026-06-01）。本 ADR の中心判断「HTTP transport を canonical、
PCSC を legacy」は、作業者が PC/SC 経路の位置づけを誤って想定したことによる誤決定だった。
正しくは **PC/SC 経路が本筋**で、ESP32-S3 は USB CCID として PN5180 を公開する。詳細は
ADR-0008。本 ADR は history として残し、書き換えない（docs-as-code 流儀）。

## Date

2026-06-01

## Context

RFID 読み取りハードウェアを **PN532 + ESP32（WROOM-32）** から **PN5180 + ESP32-S3** に
切り替える仕様変更が降りた。これは単なる MCU 差し替えではなく、reader IC のプロトコル世代
変更を伴う。Python 側のコード境界・既存契約（HTTP transport / `reader_id` / `tag_id` 表記 /
`rfid_cards.json`）にどこまで波及させるかを先に決めておかないと、firmware と Python の
drift が起きる。

forces:

- **PN5180 は ISO 15693（vicinity, UID 8B）+ 14443 A/B 対応**。PN532 は 14443 A/B + FeliCa で
  15693 非対応。タグ UID 長が変わる（最大 8B）。
- **配線が I2C（TCA9548A ×2）→ SPI（CS-line 多重 or 個別バス）に変わる**。firmware・配線図の
  再設計が必要。Python 側は無関係。
- **ESP32-S3 は USB-OTG / 多 GPIO**。firmware ビルド設定が変わるが、Python 側からは透過。
- **PC/SC 直結経路（`rfid/reader_thread.py` + pyscard, ACS ACR122U 想定）は 14443 専用**で、
  PN5180 を PC/SC 越しに読む標準モジュールは少ない。実用上 legacy 化する。
- **HTTP transport（`rfid/http_receiver.py`）の JSON 契約**（`reader_id` / `tag_id` /
  `timestamp`, `POST /rfid`, `GET /status`）を維持できれば、Python 受信側はほぼ無改修で済む。
- **`reader_id` 命名規約**（`seat_1..9` / `board_1..5`）と confidence 行列・integration エンジンは
  hardware 非依存で温存可能。

関連: `rfid/http_receiver.py`, `rfid/reader_thread.py`, `rfid/card_master.py`, `config_default.json`,
CLAUDE.md § 技術スタック / 実装状況 / エラーハンドリング方針。

## Decision

1. **HTTP transport を canonical（唯一推奨）とする**。ESP32-S3 firmware が WiFi で
   `POST /rfid` JSON を Python `RFIDHTTPReceiver` に投げる構成を第一系統に固定する。
2. **PCSC 直結経路（`rfid/reader_thread.py` / `rfid/bridge.py`）は legacy に降格**。コードは
   当面残置するが、CLAUDE.md / docstring / config コメントで「**PN5180 では非対応**」と明示し、
   新規構築での選択肢から外す。完全削除は別タスクで判断する（ADR を起こしてから）。
3. **HTTP JSON 契約は不変**: `POST /rfid` body の `reader_id` / `tag_id` / `timestamp` キー名・
   セマンティクス、`reader_id` 命名規約（`seat_1..9` / `board_1..5`）、`GET /status` の挙動は
   そのまま維持する。firmware 側はこの契約に追従する。
4. **`tag_id` の表記は colon-separated upper-hex を維持**し、**UID 長は 4 / 7 / 8 バイトすべて許容**
   する（PN5180 ISO 15693 は 8B、過去資産の 4B/7B も読み込めるよう緩い regex）。
   `card_master.normalize_tag_id` がこの方針に沿うことを別タスクで確認・テスト追加する。
5. **`rfid_cards.json` の形式は不変**（`{"cards": {"<UID>": "<card_code>"}}`）。既存登録の
   再投入は運用判断。
6. **firmware ↔ Python 契約の固定は ISSUE-0006 に集約**し、本 ADR では「契約を破らない」原則のみ
   置く。firmware 仕様の最終確定（status エンドポイント詳細・エラーレスポンス・heartbeat 等）は
   別途 issue で追跡する。

本 ADR は **方針のみ** を確定し、コード・config・テストの実改修は別タスク（docs/worklog で
段階的に進める）。

## Alternatives Considered

- **Alternative A — HTTP も PCSC も両系統サポート継続**
  - Pros: 既存 ACS ACR122U 利用者の移行不要。
  - Cons: PN5180 で PCSC 経路は事実上使えず、両系統メンテのコストが見合わない。混乱を招く。
  - Why rejected: 「PN5180 + ESP32-S3 へ移行」という今回の仕様変更と整合しない。

- **Alternative B — HTTP JSON 契約を 15693 用に新スキーマへ変更**（UID を bytes 長で型付け等）
  - Pros: 新ハードウェアにフィットした正規化。
  - Cons: 既存 receiver / card_master / config / tests と breaking。firmware 側が完成して
    いない段階で先行変更すると drift する。
  - Why rejected: HTTP JSON はあくまで「reader 種別非依存の中継」。UID 長違いは tag_id 文字列の
    長さで自然に吸収できる。premature 変更を避ける。

- **Alternative C — PCSC 経路を即時削除**
  - Pros: コード簡素化。
  - Cons: テスト・ドキュメント・config の連動削除が広く、判断材料（既存利用者の有無）が揃って
    いない段階の破壊的変更。
  - Why rejected: legacy 降格で十分。完全削除は別 ADR で判断する。

## Consequences

- Positive
  - Python 受信側の改修が最小（contract 維持）。
  - firmware 側の変更が hardware 直近に閉じる。
  - 新規利用者は HTTP transport 一択で迷わない。
- Negative / trade-offs
  - PCSC 経路コードが「動くが推奨しない」状態で残るため、将来削除タスクを忘れないよう
    ISSUE で追跡する必要。
- Neutral / new constraints
  - `tag_id` の UID 長許容範囲が広がる（4/7/8B）。card_master の正規化テストを増やす。
  - firmware ↔ Python の API 契約は ISSUE-0006 で固定し続ける必要。

## Validation / Follow-up

- [x] CLAUDE.md（プロジェクト概要 / 技術スタック / 実装状況 / ディレクトリ構成 docstring）を本 ADR に追従。
- [x] ISSUE-0006（firmware ↔ Python API 契約の固定）を Open で登録。
- [ ] `card_master.normalize_tag_id` が 8B UID（colon-hex / 連結 hex）を許容することの確認 + 単体テスト追加。
- [ ] `rfid/reader_thread.py` / `rfid/bridge.py` の docstring と `config_default.json` のコメントに
      「PN5180 では非対応」を明示。
- [ ] `tests/test_rfid_http.py` に PN5180 想定の 15693 UID（8B）fixture を追加。
- [ ] PCSC 経路の完全削除を判断する別 ADR（必要なら ADR-0008）。

## Related Files

- `rfid/http_receiver.py`（canonical transport, 契約維持）
- `rfid/reader_thread.py` / `rfid/bridge.py`（legacy 降格対象）
- `rfid/card_master.py`（UID 長 4/7/8B 許容の確認対象）
- `config_default.json`（PCSC コメントの更新対象）
- `rfid_cards.json`（形式不変、再登録は運用判断）
- `core/events.py`（`RFIDEvent` 不変）
- `integration/engine.py`（confidence 行列不変）

## Related Tests

- `tests/test_rfid_http.py`（HTTP 契約。15693 UID fixture を追加予定）
- `tests/test_rfid.py`（PCSC 経路。legacy 化に伴う扱いを ISSUE-0006 で追跡）

## Related Commits

- 本 ADR と同じコミット（RFID hardware migration planning）。

## Supersedes / Superseded by

- Supersedes: —
- **Superseded by: ADR-0008**（PN5180 + ESP32-S3 via USB CCID — PC/SC is the canonical RFID transport）
- 関連: ISSUE-0006（本 ADR と同じ前提で起こした issue。ADR-0008 で ISSUE-0007 に倒した）
