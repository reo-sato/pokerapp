# ADR-0015: PN5180 + ESP32-S3 via USB CCID — PC/SC is the canonical RFID transport

## Status

Accepted

## Date

2026-06-01

## Context

ADR-0014 で「HTTP transport を canonical、PC/SC は legacy」と決めたが、これは作業者
（Claude）が PC/SC 経路の位置づけを誤って想定したことが原因の誤決定だった。**正しくは PC/SC
経路（`rfid/reader_thread.py` + pyscard）が本筋**であり、新ハードウェア構成では:

- **ESP32-S3 の native USB が USB CCID class を実装**し、配下の PN5180 ×N を PC/SC 互換
  リーダーとしてホスト PC に公開する。
- ホスト側 Python は **pyscard** 越しに PC/SC API でカードを読む（`rfid/reader_thread.py` /
  `rfid/bridge.py` の経路）。
- **WiFi / HTTP は不要**（ある必要がない構成）。`rfid/http_receiver.py` 経路は本筋ではない。

これにより ADR-0014 は前提が誤りだったため Superseded に倒し、本 ADR を Accepted で起こす。

forces:

- **配線・通信単純化**: USB CCID なら OS 標準 PC/SC スタック（`pcscd` / WinSCard）に乗り、
  WiFi 設定・HTTP server を持たずに済む。
- **pyscard 既存実装の温存**: `rfid/reader_thread.py` / `rfid/bridge.py` の構造（`reader_configs`
  に reader 名と role/seat を持つ）はそのまま使える。
- **multi-reader**: USB CCID は **1 デバイス複数 slot** を仕様で許容する。1 つの ESP32-S3
  composite device 配下に PN5180 ×N を「slot」として公開すれば、pyscard 側からは
  「reader_name 配列」として自然に列挙される。
- **タグ世代**: PN5180 は ISO 15693（UID 8B）に対応。pyscard の `connection.getATR()` /
  `transmit(APDU)` は ATR + APDU 抽象を返すため、UID 表現は PCSCBridge 側で 4/7/8B いずれも
  許容できる（実装確認は follow-up）。
- **整合**: confidence 行列 / `RFIDEvent` / `reader_id` 命名（`seat_1..9` / `board_1..5`）は
  hardware 非依存で不変。

関連: `rfid/reader_thread.py`, `rfid/bridge.py`, `rfid/card_master.py`, `rfid/http_receiver.py`,
`config_default.json`, ADR-0014（Superseded）。

## Decision

1. **PC/SC（pyscard）経路を canonical（本筋）とする**。`config_default.json` のデフォルト
   `rfid.transport` は `"pcsc"` を前提とし、`rfid/reader_thread.py` を第一系統に位置づける。
2. **ESP32-S3 firmware は USB CCID class を実装** し、配下の PN5180 ×N を **USB CCID multi-slot**
   としてホスト PC に公開する。host 側は OS 標準 PC/SC スタック越しに pyscard が `reader_name`
   配列として列挙する。
3. **`reader_configs` の reader 名 ↔ 役割マッピング契約を維持**: `[{"name": "...", "role": "seat",
   "seat": N}]` / `{"role": "board"}` の構造を変えない。reader_name 文字列は firmware の
   CCID slot name に追従する（firmware 側で固定する → ISSUE-0015 で追跡）。
4. **WiFi / HTTP transport（`rfid/http_receiver.py`）は optional secondary に降格** し、
   debug / remote / 分散設置などの限定用途で残置する。完全削除は別 ADR で判断（即時削除しない）。
5. **タグ UID 表記**: `card_master.normalize_tag_id` が 4 / 7 / **8 バイト** すべてを colon-hex
   upper に正規化することを follow-up で確認・テスト追加。`rfid_cards.json` 形式は不変。
6. **ADR-0014 は Superseded**。本 ADR が同一の対象（PN5180 + ESP32-S3 移行）に対する正しい判断。
   ADR-0014 を削除はせず、history として残す（CLAUDE.md docs-as-code 流儀）。
7. **firmware ↔ host の契約固定は ISSUE-0015 に集約**: USB CCID descriptor / vendor strings /
   reader_name / slot 配置 / ATR（ISO 15693 用 proxy ATR）/ pseudo-APDU セット / card
   inserted-removed 通知。ISSUE-0014（HTTP 契約）は本 ADR で Superseded by ISSUE-0015 に倒す。

本 ADR は **方針確定のみ**。コード・config・テストの実改修は別タスク。

## Alternatives Considered

- **Alternative A（採用）— ESP32-S3 が USB CCID として PN5180 を公開、host は pyscard / PC/SC**
  - Pros: OS 標準 PC/SC スタックに乗る。pyscard 既存実装をそのまま canonical 化できる。
    WiFi 設定不要。
  - Cons: ESP32-S3 firmware で USB CCID class を正しく実装する必要（vendor 実装責務が増える）。
  - Why chosen: 「PC/SC が本筋」という user 確定方針と整合し、Python 側コード境界の改修が
    最小になる。

- **Alternative B — 別途の USB CCID リーダー（ACS 等）を本筋にし、PN5180 + ESP32-S3 は補助系統**
  - Pros: 既存 ACS リーダー資産が使える。
  - Cons: PN5180 + ESP32-S3 を採用する今回の仕様変更の意義が薄れる。
  - Why rejected: 「ESP32-S3 + PN5180 を本筋として PC/SC で見せる」という構成意図と矛盾。

- **Alternative C — USB シリアルで生イベントを送り、Python 側で PC/SC 風 API を被せる**
  - Pros: USB CCID 実装の手間を回避。
  - Cons: 物理 PC/SC ではないので OS スタックの恩恵（reader 列挙 / hot-plug / card
    inserted-removed 通知）を失い、独自プロトコルを保守する必要。
  - Why rejected: 「PC/SC 経路を本筋に」という方針から最も離れる。

- **Alternative D（ADR-0014 採用案）— HTTP を canonical、PCSC を legacy**
  - Why rejected: PC/SC 経路は本筋であり、HTTP は本筋ではない（user 方針）。本 ADR で
    ADR-0014 を Superseded に倒す。

## Consequences

- Positive
  - Python 側は `rfid/reader_thread.py` 既存実装をそのまま canonical として使える。
    `main.py` のデフォルト起動経路も自然。
  - OS 標準 PC/SC スタックの hot-plug / card inserted-removed 通知に乗れる。
  - WiFi / HTTP サーバを運用しなくて済む（攻撃面が狭い）。
- Negative / trade-offs
  - ESP32-S3 firmware が USB CCID class（multi-slot）を仕様準拠で実装する必要。host OS の
    PC/SC スタックとの相互運用を実機で検証する必要。
  - HTTP 経路コード（`rfid/http_receiver.py`）が optional secondary として残るため、二重保守の
    薄いコストが発生する。
- Neutral / new constraints
  - reader_name 文字列が firmware の CCID slot name に依存する（contract 化要 → ISSUE-0015）。
  - tag UID 長 4/7/8B 許容は ADR-0014 から引き継ぐ（PCSCBridge / card_master 側の確認 follow-up）。

## Validation / Follow-up

- [x] ADR-0014 を Superseded に更新し、本 ADR で Superseded by を相互リンク。
- [x] ISSUE-0014 を Superseded by ISSUE-0015 に倒し、本 ADR の方針に追従する新 issue（ISSUE-0015,
      USB CCID firmware 契約）を Open で登録。
- [x] CLAUDE.md（プロジェクト概要 / ディレクトリ docstring / 技術スタック / 実装状況 /
      エラーハンドリング方針）を「PC/SC canonical / HTTP optional」に flip。
- [x] decision-log.md に ADR-0015 / ISSUE-0015 を追加、ADR-0014 / ISSUE-0014 を Superseded に。
- [ ] `rfid/reader_thread.py` / `rfid/bridge.py` の docstring と `config_default.json` のコメントを
      「PCSC が canonical」に書き直す小改修（次タスク）。
- [ ] `card_master.normalize_tag_id` が 8B UID を許容することの確認 + 単体テスト追加。
- [ ] `tests/test_rfid.py` に PN5180 想定（8B UID）の PCSC bridge fixture を追加。
- [ ] ESP32-S3 firmware の USB CCID descriptor / reader_name / ATR を ISSUE-0015 に固定。
- [ ] `rfid/http_receiver.py` を optional secondary としてコメント化（即時削除はしない）。
- [ ] 将来: PCSC API 契約（reader_name 命名 / pseudo-APDU セット）の `docs/contracts/` 化を検討。

## Related Files

- `rfid/reader_thread.py` / `rfid/bridge.py`（canonical）
- `rfid/card_master.py`（UID 長 4/7/8B 許容の確認対象）
- `rfid/http_receiver.py`（optional secondary に降格）
- `config_default.json`（デフォルト `transport=pcsc` 方針）
- `rfid_cards.json`（形式不変）
- `core/events.py` / `integration/engine.py`（不変）

## Related Tests

- `tests/test_rfid.py`（PCSC 経路。8B UID fixture を follow-up で追加）
- `tests/test_rfid_http.py`（HTTP 経路。optional secondary 扱い）

## Related Commits

- 本 ADR と同じコミット（RFID hardware migration re-planning, PCSC canonical pivot）。

## Supersedes / Superseded by

- **Supersedes: ADR-0014**（HTTP canonical の判断は誤りだった）
- Superseded by: —
- 関連: ISSUE-0015（USB CCID firmware 契約）、ISSUE-0014（本 ADR で Superseded）

---

## v2 確定事項 追記 (2026-06-22)

別口で進めている firmware 担当との意識合わせの結果、現場ハードウェア構成の正典として
「ポーカーテーブル RFID システム計画書 v2 (2026-06-22)」が確定した。本 ADR の判断は維持しつつ、
以下を additive に確定する。

- **HTTP 経路の位置付け**: 本 ADR で「optional secondary」としていた HTTP 経路は v2 計画書で
  **deprecated**（production 使用不可）に降格。`rfid/http_receiver.py` のコードは debug / CI 互換のため
  当面残置するが、新規環境では `rfid.transport="http"` / `readers` dict 構成を使用しない。
- **物理構成**: PN5180 リーダー **13 台**（席 1..8 = 8 台 + ボード 1..5 = 5 台）を ESP32-S3 制御基板
  1 枚に集約し、ESP32-S3 の **native USB-OTG ポート**経由で 1 本の USB ケーブルで PC に直結する。
  UART ブリッジ IC（CP2102N / CH340 等）経由は COM ポート化のため CCID 認識不可。
- **HTTP-related config**: `rfid.transport="http"` および `config_default.json` の `readers` dict は
  新規環境では使用しない（残置はテスト互換のため）。canonical は常に `pcsc_readers` (list)。

詳細な GPIO 配線（NSS×13 / SPI / MUX）は ADR-0034 末尾 + `firmware/esp32s3-pn5180-ccid/app_config.h`
を参照。

### 追補 (2026-06-22 後): HTTP 経路は frozen — 新機能追加なし

`rfid/http_receiver.py` + `config.rfid.transport="http"` / `readers` dict は **frozen**：

- バグ修正・既存テスト互換のための変更のみ受け付ける。
- 新機能（新 endpoint / 新 event field / 新 reader_id 拡張等）は追加しない。
- 削除タイミングは別 ADR で再評価する（本 ADR では「残置」とした）。
- 物理層の真実は `docs/hardware/pn5180-esp32s3-wiring.md` に source of truth を確定。
