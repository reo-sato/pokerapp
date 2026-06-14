# Worklog: RFID USB CCID firmware↔host 契約の凍結（ADR-0034 / ISSUE-0015）

## Date

2026-06-14

## Scope / Task

ISSUE-0015（ADR-0015 の follow-up）: PN5180 + ESP32-S3 の USB CCID firmware と host Python（PC/SC）の
境界契約を凍結する。firmware は別 repo だが、契約凍結と host 側準拠・回帰テストは実機なしで可能。

## Goal

USB descriptor / reader_name / ATR / pseudo-APDU / UID / hot-plug の境界を normative に固定し、drift を防ぐ。
host を契約に準拠させ（config 形・UID 正規化）、回帰テストで固定する。

## Investigation / Findings

- ドリフト: `config_default.json` の `rfid.readers` は **dict**（HTTP 受信 `rfid/http_receiver.py` 用）だが、
  canonical PC/SC `RFIDThread` は **list**（`[{name, role, seat}]`）を期待。`transport="pcsc"` で既定 config を
  使うと dict を list として iterate して **壊れる**（latent bug）。
- `card_master.normalize_tag_id` / `bytes_to_tag_id` は任意長 hex を扱えるが **8B UID（ISO 15693）の回帰が無い**。
- host `rfid/bridge.py` は既に Get UID `FF CA 00 00 00` を実装、ATR は解釈しない（ATR-agnostic）。

## Changed Files

- `docs/contracts/rfid-usb-ccid.md`（新規, v1.0 frozen）: USB CCID / VID-PID / reader_name 規約 / slot↔役割
  （host config が source of truth）/ ATR（host は ATR-agnostic）/ Get UID pseudo-APDU / UID 4-7-8B 正規化 /
  hot-plug（PC/SC polling）/ multi-platform / versioning。
- `docs/adr/0034-rfid-usb-ccid-firmware-host-contract-freeze.md`（新規, Accepted）: freeze 判断 + normative 選択。
- `config_default.json`: `rfid.pcsc_readers`（list, PN5180+ESP32-S3 サンプル）+ コメント追加。
- `main.py`: pcsc 経路を `pcsc_readers` 優先（list でなければ空）に変更（latent crash 解消）。
- `tests/test_rfid.py`: `TestIso15693Uid8Byte`（8B roundtrip / lookup）+ RFIDThread の board マッピング /
  8B UID flow テスト。
- `docs/issues/0015-...md`: Status → Fixed + Fix 詳細。
- docs: decision-log（ADR-0034）/ CLAUDE.md（実装状況 2 行 + 残作業 #3）/ CHANGELOG。

## Key Decisions（ADR-0034）

- 契約を `docs/contracts/` に凍結（contract-first）。**役割→reader_name は host config が source of truth**、
  firmware は slot 順序の安定のみ保証。**host は ATR-agnostic**、Get UID pseudo-APDU のみに依存。
- pcsc は `pcsc_readers`(list) に分離（http の `readers` dict と区別）→ 型ドリフトの latent bug 解消。
- firmware の VID/PID・実 reader_name はプレースホルダ。実機実装時に契約 §2/§4 へ追記（host コードは
  reader_name 等値照合のみなので契約安定なら host 変更不要）。

## Expected / Implemented Behavior

- 8B UID（ISO 15693）が `RFIDEvent.tag_id` まで長さ非依存で正規化されて流れる。
- `transport=pcsc` は `pcsc_readers`(list) を読む。dict 誤設定では空 list で安全（クラッシュしない）。
- host の RFID 読み取りロジックは不変（既存 Get UID / 正規化）。

## Test Results

- `pytest tests/test_rfid.py -q` — 33 passed（既存 + 5 新規）。
- `pytest tests/ --ignore=tests/test_vision.py -q` — **623 passed, 0 skipped**（既存 618 + 5）。
- `ruff check .` — clean。`config_default.json` JSON 妥当。

## Mismatches Found During Testing

- config(dict) vs RFIDThread(list) の型ドリフトを発見 → `pcsc_readers`(list) 分離 + main.py フォールバックで解消。

## Remaining Gaps / Out-of-Scope（実環境）

- firmware の VID/PID・実 reader_name 確定 → 契約 §2/§4 追記。
- live hot-add（実行中 reader 追加/名称変化追従）= future（v1.0 は起動時 connect のみ）。
- 実機 E2E（Phase H）。

## Related ADRs / Issues

- ADR-0034（本件）/ ADR-0015（PC/SC canonical）/ ISSUE-0015（Fixed）/ ISSUE-0014（Superseded）

## Related Commits

- 本 commit
