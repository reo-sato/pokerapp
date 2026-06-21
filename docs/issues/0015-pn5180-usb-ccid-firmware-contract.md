# Issue 0015: ESP32-S3 (PN5180) USB CCID firmware contract for the canonical PC/SC path

## Date

2026-06-01

## Status

Fixed（2026-06-14, ADR-0034。契約を `docs/contracts/rfid-usb-ccid.md` v1.0 として凍結 + host 準拠 +
回帰テスト。firmware の確定値（VID/PID・実 reader_name）は実機実装時に契約 §2/§4 へ追記。§ Fix 参照）

## Severity / Priority

- Severity: Medium（PC/SC が canonical（ADR-0015）になったため、firmware が USB CCID 仕様準拠で
  公開する内容が決まらないと host 側 `rfid/reader_thread.py` の `reader_configs` を確定できない）
- Priority: P2

## Area

rfid / firmware boundary / pcsc / usb-ccid

## Expected Behavior

ADR-0015 の方針に従い、ESP32-S3 firmware が PN5180 を **USB CCID class** で公開し、ホスト PC の
PC/SC スタック越しに pyscard が安定した形で列挙・読み取りできる:

- **USB descriptors**: VID/PID（ベンダー範囲）、manufacturer / product 文字列が固定。
- **CCID multi-slot**: 1 composite device 配下に **PN5180 ×N slot**。各 slot が 1 物理リーダー
  に対応。OS から見える reader_name 文字列が **slot ごとに一意で安定**。
- **reader_name ↔ 役割マッピング契約**: `seat_1..9` / `board_1..5` への対応規則（例: 末尾
  `Interface 0..13` を slot index にして config 側でマップ）。
- **ATR (Answer-to-Reset)**: ISO 15693 用の **proxy ATR**（PC/SC v2.01 Part 3 Storage Card 仕様
  互換 or vendor 定義）を slot から返す。host pyscard 側で識別可能なこと。
- **pseudo-APDU / GET UID**: PC/SC v2.01 Part 3 の `FF CA 00 00 00`（Get Data: UID）等の
  pseudo-APDU で UID（4 / 7 / **8 バイト**）が取れること。
- **card inserted / removed 通知**: PC/SC 標準の status polling で hot-plug 検出が機能する。
- **error handling**: WiFi 切断などの旧懸念は本構成では消える代わりに、USB 再列挙時の挙動
  （reader_name 変化 / hot-plug 再認識）を確認。
- **multi-platform**: Linux `pcscd` / macOS / Windows WinSCard で互換動作。

## Actual Behavior

- 既存 `rfid/reader_thread.py` は `pyscard` + `reader_configs=[{"name": "...", "role": "seat",
  "seat": N}]` という抽象を持つ（PN532 + 別 PC/SC リーダー時代の名残）。
- PN5180 + ESP32-S3 firmware は本 repo 外で開発中。USB CCID descriptor / reader_name の確定形が
  未確定。
- `card_master.normalize_tag_id` は colon-hex / 連結 hex を許容するが、8B UID に対する単体テストが
  まだ無い。
- pyscard の hot-plug 列挙 / status 監視を `rfid/bridge.py` がどこまで使っているかも、新ハード
  での実機で再検証が必要。

## Reproduction

仕様レビュー（バグではなく ADR-0015 の follow-up）:

1. `rfid/reader_thread.py` の `reader_configs` 解釈と `rfid/bridge.py` の PCSC API 利用部を読む。
2. `rfid/card_master.normalize_tag_id` を読み、8B UID に対するテストが無いことを確認。
3. PN5180 + ESP32-S3 firmware は別 repo（未公開 or 別ブランチ）。

## Root Cause

ADR-0015 で「USB CCID で PC/SC として公開」という構成を採用したが、firmware 側の **USB
descriptor / reader_name / ATR / pseudo-APDU セット** などホスト側が期待する細部が文章化されて
いない。これらは host pyscard コードと firmware の二箇所で実装される境界であり、drift を防ぐ
ために契約として固定する必要がある。

## Fix

**Fixed（2026-06-14, ADR-0034）**: 契約を凍結し host を準拠させた:

- **契約凍結**: `docs/contracts/rfid-usb-ccid.md` v1.0 — USB CCID class / VID-PID / reader_name 安定規約 /
  slot↔役割（host config が source of truth）/ ATR（host は ATR-agnostic）/ pseudo-APDU（Get UID
  `FF CA 00 00 00` のみ）/ UID 4-7-8B 正規化 / hot-plug（PC/SC polling）/ multi-platform / freeze 規則。
- **config**: `config_default.json` に `pcsc_readers`（**list**, PN5180 + ESP32-S3 想定）サンプル + コメント
  追加。HTTP 用 `readers`（dict）とキー分離。`main.py` の pcsc 経路を `pcsc_readers` 優先に変更し、dict 誤設定
  での latent crash（dict を list として iterate）を解消。
- **8B UID 回帰**: `tests/test_rfid.py` に 8B UID（ISO 15693）の `bytes_to_tag_id`/`normalize_tag_id`
  roundtrip + `CardMaster` lookup + `MockPCSCBridge → RFIDThread → RFIDEvent` + board(role/index) マッピング。
- **残（実環境）**: firmware の VID/PID・実 reader_name を確定して契約 §2/§4 に追記。live hot-add は future。
  - bring-up 診断 `tools/probe_pcsc.py`（`list` で実 reader_name を確認 → `check`/`watch` で §3-8 を検査）+
    手順 `docs/hardware-qa-checklist.md` を用意済（2026-06-21）。確定した実 reader_name / VID/PID はこの
    `list` 出力から契約へ転記する。

## Regression Test

未実装。Fix の一部として:

- `tests/test_rfid.py`: 8B UID（`b"\x04\xAB\xCD\xEF\x12\x34\x56\x78"` 相当）の PCSCBridge
  読み取り → `RFIDEvent` 正常化。
- `tests/test_rfid.py`（card_master）: `normalize_tag_id` の 8B colon-hex / 連結 hex roundtrip。

## Affected Files

- `rfid/reader_thread.py` / `rfid/bridge.py`（canonical 経路）
- `rfid/card_master.py`（UID 長確認）
- `tests/test_rfid.py`
- `config_default.json`（reader_configs サンプル）
- 将来: `docs/contracts/`（PC/SC reader_name / pseudo-APDU）

## Related Worklog

- `docs/worklog/2026-06-01-pn5180-pcsc-canonical-pivot.md`

## Related ADRs

- `docs/adr/0015-pn5180-esp32s3-usb-ccid-pcsc-canonical.md`（本 issue を生む方針）
- `docs/adr/0014-migrate-rfid-to-pn5180-esp32s3-and-canonical-http-transport.md`（Superseded）

## Related Commits

- 本 issue と同じコミット（PCSC canonical pivot）。

## Notes

ISSUE-0014（HTTP API 契約）は本 issue で Superseded。HTTP 経路は ADR-0015 で optional secondary
に降格しており、debug / remote 用途に限り `rfid/http_receiver.py` を残すが、本筋契約は本 issue で
追跡する USB CCID / PC/SC 側。
