# ADR-0034: RFID USB CCID firmware ↔ host (PC/SC) 契約の凍結

## Status

Accepted（**契約 freeze**。`docs/contracts/rfid-usb-ccid.md` v1.0 を正準とし、host 側の準拠と
回帰テストを整える。firmware の確定値は別 repo で実装時に契約へ追記）

## Date

2026-06-14

## Context

ADR-0015 で「PN5180 + ESP32-S3 を **USB CCID** で公開し、host は **PC/SC（pyscard）**で読む」構成を
canonical に採用した。しかし firmware（別 repo・別チームで開発）と host Python の境界
（USB descriptor / reader_name / ATR / pseudo-APDU / UID / hot-plug）が文章化されておらず、drift する
リスクが ISSUE-0015 として open のままだった。

加えて実装上のドリフトが残っていた:

- `config_default.json` の `rfid.readers` は **dict**（`{reader_id: {role,seat}}`）で、これは HTTP 受信
  （`rfid/http_receiver.py`, `reader_configs: dict`）向け。一方 canonical な PC/SC `RFIDThread` は
  **list**（`[{name, role, seat}]`）を期待する。`transport="pcsc"` で既定 config を使うと dict が list として
  iterate され **壊れる**（latent bug）。
- `card_master.normalize_tag_id` は任意長 hex を許容するが **8B UID（ISO 15693）の回帰テストが無い**。

firmware が手元に無くても、(1) 境界契約の凍結、(2) host 側の準拠（config 形・UID 正規化）と回帰テストは
**実機なしで**確定できる。

## Decision

1. **firmware↔host 契約を `docs/contracts/rfid-usb-ccid.md` v1.0 として凍結**する（ISSUE-0015 の
   docs/contracts 化）。normative 内容（MUST/SHOULD）の要点:
   - device は **USB CCID class** を実装（HID/vendor 不可）。VID/PID と manufacturer/product 文字列を固定。
   - PN5180 1 個 = CCID **1 slot**。reader_name は slot ごとに **一意・安定**（product 文字列 + slot index が
     再列挙を跨いで不変）。slot 順序は firmware が固定。
   - **役割（seat/board）→ reader_name の対応は host config が唯一の source of truth**（`pcsc_readers` list）。
     firmware は slot 順序の安定のみ保証する。
   - **ATR**: firmware は PC/SC 互換 ATR を返す（PC/SC connect 成立用）。**host は UID 読み取りに特定 ATR
     バイトを前提にしない**（forward-compat）。
   - **pseudo-APDU**: host が依存するのは **Get UID = `FF CA 00 00 00` → UID + `90 00`** のみ。
   - **UID 長 4/7/8B** を長さ非依存で扱い、大文字コロン区切り hex に正規化。
   - hot-plug は PC/SC status polling、live hot-add は v1.0 対象外（future）。multi-platform 対応。
2. **config の正準形を確定**: PC/SC 経路は `config.rfid.pcsc_readers`（**list**）を使う。HTTP 経路の
   `readers`（**dict**）とはキーを分離する。`config_default.json` に **PN5180 + ESP32-S3 想定の
   `pcsc_readers` サンプル**を追記（コメント付き）。`main.py` の pcsc 経路は `pcsc_readers` を優先し、dict が
   渡る誤設定では空 list にフォールバック（latent bug の解消）。
3. **host 側の準拠を回帰テストで固定**（実機なし）: 8B UID の `bytes_to_tag_id`/`normalize_tag_id` roundtrip、
   8B UID の `MockPCSCBridge → RFIDThread → RFIDEvent`、board(role/index) マッピング。
4. **firmware の確定値（VID/PID、実 reader_name）**は別 repo の実装時に確定し、契約 §2/§4 に追記する
   （host コードは reader_name 等値照合のみなので **契約が安定していれば host 変更不要**）。

## Alternatives Considered

- **契約を作らず実機で詰める** → firmware と host の二箇所実装で drift。実機が来てからでは手戻り大。
  → 先に契約凍結（contract-first）。
- **HID / vendor-specific で RFID 公開** → OS 標準 PC/SC に乗らず host が独自ドライバを持つことになる。
  → CCID（ADR-0015 を踏襲）。
- **役割マッピングを firmware（reader_name 命名）に持たせる** → firmware 変更で host が壊れる。
  → host config を source of truth、firmware は slot 順序の安定のみ。
- **`readers` dict を pcsc でも流用** → list/dict の型ドリフトで壊れる。→ `pcsc_readers`(list) に分離。

## Consequences

- Positive: firmware と host が独立に実装でき、境界が drift しない。実機なしで host 準拠を凍結。pcsc の
  latent bug（dict→list）を解消。8B UID の回帰を獲得。ISSUE-0015 を Fixed にできる。
- Negative / trade-offs: firmware の VID/PID・実 reader_name は実装時に契約へ追記が必要（プレースホルダ）。
  live hot-add / ATS など一部は v1.0 対象外（future, additive）。
- Neutral: `config_default.json` に `pcsc_readers` サンプル追加、`main.py` pcsc 経路を 1 箇所調整。host の
  RFID 読み取りロジック自体は不変（既に Get UID / UID 正規化を実装済み）。

## Validation / Follow-up

- [x] `docs/contracts/rfid-usb-ccid.md` v1.0（USB/CCID/reader_name/ATR/pseudo-APDU/UID/hot-plug/freeze）。
- [x] `config_default.json` に `pcsc_readers` サンプル + コメント。`main.py` pcsc 経路を `pcsc_readers` 優先に。
- [x] `tests/test_rfid.py`: 8B UID roundtrip / 8B `MockPCSCBridge→RFIDThread→RFIDEvent` / board マッピング。
- [x] ISSUE-0015 を Fixed に更新（契約リンク）。ISSUE-0014（HTTP 契約）は Superseded のまま。
- [ ] （実環境）firmware の VID/PID・実 reader_name 確定 → 契約 §2/§4 追記。live hot-add（future）。

## Related Files

- `docs/contracts/rfid-usb-ccid.md`（本契約）/ `config_default.json`（`pcsc_readers`）/ `main.py`（pcsc 経路）
- `rfid/bridge.py` / `rfid/reader_thread.py` / `rfid/card_master.py` / `tests/test_rfid.py`

## Related Tests

- `tests/test_rfid.py`（8B UID / RFIDThread / board マッピング）

## Related Commits

- 本 ADR の実装 commit（2026-06-14）

## Supersedes / Superseded by

- Supersedes: —（ADR-0015 の follow-up = ISSUE-0015 を解決。関連: ADR-0015 / ISSUE-0015 / ISSUE-0014(Superseded)）
- Superseded by: —
