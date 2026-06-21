# Worklog: 実機 RFID（PC/SC canonical）bring-up 診断ツール + 手順

## Date

2026-06-21

## Scope / Task

実機として準備した RFID センサ（ESP32-S3 + PN5180, USB CCID → PC/SC canonical, ADR-0015/0034）を
テストするための bring-up 診断ツールと手順書を追加する（Phase H 実機 E2E の前段。音声環境は不要）。

## Goal

- 実機 RFID を `docs/contracts/rfid-usb-ccid.md` v1.0 の MUST に対して検査できる CLI を用意する。
- production と同じコード経路（`rfid.bridge.PCSCBridge` / `rfid.reader_thread.RFIDThread`）を叩き、
  「ツールで OK なら hand logger でも OK」を成立させる。
- pyscard / 実機が無い CI でも回せるテストで、ツールのロジックを回帰ロックする。
- 操作手順を `manual-qa-checklist.md`（実機なし）と分けて文書化する。

## Changed Files

- `tools/probe_pcsc.py`（新規）— 実機 PC/SC 診断 CLI。`list` / `check` / `watch` の 3 サブコマンド +
  純粋ロジック（`match_readers` / `lint_pcsc_readers` / `analyze_uid` / `reader_label` / `format_event`）+
  DI シーム（`probe_connect` / `run_watch` が `bridge_factory` 注入可）+ 非破壊 config ロード。
- `tests/test_tools_probe_pcsc.py`（新規）— 35 件。pyscard/実機なしで純粋ロジック + DI + コマンド層を検証。
- `docs/hardware-qa-checklist.md`（新規）— Phase H 実機 RFID QA 手順（契約 §↔手順の受け入れ基準表つき）。
- `CLAUDE.md` — よく使うコマンドに probe_pcsc 3 行追加、実機 E2E (Phase H) 行 + 残作業 #2 を更新。
- `CHANGELOG.md` — Unreleased に Added エントリ。
- `docs/issues/0015-...md` — 実環境残作業に probe_pcsc / hardware-qa-checklist への導線を追記。

## Expected Behavior

- `list`: 接続中 reader_name を列挙し、config.rfid.pcsc_readers と **等値**で突き合わせて
  matched/MISSING/unconfigured を表示（契約 §3-4/§8）。
- `check`: config を lint（role/seat/index・重複・name 欠落）し、各 reader に connect 検査して
  PASS/FAIL（§4-5, カード不要）。全 PASS かつ lint クリーンで終了コード 0、それ以外 1。
- `watch`: 実 RFIDThread を起動し、タップごとに role/seat/board_index・正規化 UID（4/7/8B 判定）・
  card 解決を 1 行表示（§6-8: Get UID / UID 正規化 / デバウンス hot-plug）。
- pyscard 未導入時は `list`/`check`/`watch` とも導線（`pip install ".[pcsc]"`）を出して終了コード 2。

## Implemented Behavior

期待どおり。ポイント:

- **production 経路の再利用**: `watch` は `RFIDThread` を `bridge_factory=PCSCBridge` で起動して
  rfid_queue を drain するだけ。role/seat/board_index/デバウンスの挙動は hand logger 実行時と同一。
  `check`/`list` も `PCSCBridge.connect` / `rfid.bridge.list_readers` を使う（独自実装で drift しない）。
- **テスト可能性**: connect/watch は `bridge_factory` を DI でき、テストは `MockPCSCBridge` を注入。
  コマンド層は `pyscard_available` を monkeypatch + `lister`/`bridge_factory` 注入で、pyscard/実機なしでも
  wiring を検証。純粋関数（match/lint/analyze/format）は単体で検証。
- **非破壊**: config は明示パス > config.json > config_default.json の順で読むだけ（`core.config.load_config`
  と違い config.json を生成しない）。診断ツールがデータを書き換えないため。
- **契約準拠の細部**: 等値照合（前方一致しない, §4）、UID 4/7/8B を契約長として判定し他は `⚠` 表示（§7）、
  `pcsc_readers`(list) 優先で HTTP 用 `readers`(dict) は採用しない（§4, main.py と同じ）。

## Test Results

- `python -m pytest tests/test_tools_probe_pcsc.py -q` — **35 passed**。
- `python -m pytest tests/ --ignore=tests/test_vision.py -q` — **701 passed, 1 warning**
  （warning は既存の fastapi/starlette DeprecationWarning, 本変更と無関係）。
- Manual（この環境は pyscard ビルド不可 = ヘッダ無し）:
  - `python tools/probe_pcsc.py --help` — サブコマンド 3 種を表示。
  - `python tools/probe_pcsc.py list` — pyscard 未導入 → 導線を出し終了コード 2（gate 正常）。
  - 実機 + pyscard を伴う `list`/`check`/`watch` の通し確認は **operator の実機環境タスク**（本環境では不可）。

## Mismatches Found During Testing

None observed.

## Fixes Applied

- なし（新規追加のみ。既存挙動は不変 = RFID 既存テスト 22 件含む全 701 緑）。

## Remaining Gaps / Out-of-Scope

- [ ] 実機を繋いだ通し QA（`docs/hardware-qa-checklist.md` 手順1-7）= operator 環境。
- [ ] firmware の VID/PID・実 reader_name を `list` 出力から確定し、契約 §2/§4 に追記（ISSUE-0015 残）。
- [ ] live hot-add（稼働中の reader 追加追従）は契約上も future（v1.0 は起動時 connect のみ）。
- [ ] `watch` の ATR バイト列の直接表示は未実装（host は ATR 非依存 = §5、connect 成立で代替）。

## Related ADRs

- `docs/adr/0015-pn5180-esp32s3-usb-ccid-pcsc-canonical.md` — canonical PC/SC 方針。
- `docs/adr/0034-rfid-usb-ccid-firmware-host-contract-freeze.md` — 検査対象の契約 v1.0。

## Related Issues

- `docs/issues/0015-pn5180-usb-ccid-firmware-contract.md` — 実環境残作業（VID/PID・reader_name 確定）。

## Related Commits

- 本ワークログと同じコミット（probe_pcsc + hardware-qa-checklist 追加）。
