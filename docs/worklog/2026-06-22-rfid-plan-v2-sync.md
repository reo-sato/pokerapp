# Worklog: RFID 計画書 v2 を docs に反映

## Date

2026-06-22

## Scope / Task

別口で進めている友人（firmware 担当）と意識合わせを行い、現場ハードウェア構成の正典として
「ポーカーテーブル RFID システム計画書 v2 (2026-06-22)」が確定した。これを docs（CLAUDE.md /
ADR-0015 / ADR-0034 / contract / firmware checklist / CHANGELOG）に反映する docs-only タスク。

## Goal

- v2 計画書と現状 docs の差分（席数 1..9→1..8 / リーダー総数 13台 / HTTP 経路の deprecate /
  物理配線 GPIO 確定 / 既知の課題）を docs に集約する。
- コード/config 側の対応（seat range・config sample・http_receiver の最終処遇）は **本タスクの scope
  外**として残課題に明示する。

## Changed Files

- `CLAUDE.md` — 冒頭概要に「13リーダー（席8+ボード5）」追記、`rfid/http_receiver.py` を deprecated
  扱いに変更、技術スタック表の HTTP 行を deprecated に書き換え、Validation 節の `seat_no` 範囲を
  「v2 = 1..8」に修正（コード側 1..8 化は残作業と明記）、実装状況の RFID firmware 行に v2 確定事項
  （13 slot・物理配線 GPIO・落とし穴）を追記、残作業 #2 に既知課題、#3 に v2 確定を追記。
- `docs/adr/0015-pn5180-esp32s3-usb-ccid-pcsc-canonical.md` — 末尾に「v2 確定事項 追記
  (2026-06-22)」節を追加: HTTP 経路を deprecated に降格、物理構成 13台 + native USB 必須、
  HTTP-related config を新規環境で使用しない方針を明記。
- `docs/adr/0034-rfid-usb-ccid-firmware-host-contract-freeze.md` — 末尾に同様の v2 追記節:
  slot 数 13、物理配線（NSS×13 / SPI / MUX）GPIO 表、落とし穴（UART ブリッジ NG / 13台同時 RF ON NG /
  MUX SIG=47・S0=37 への変更）、既知の bring-up 課題。
- `docs/contracts/rfid-usb-ccid.md` — §1 全体像に「v2 = N=13」追記、§4 サンプルの `seat` 範囲を
  1..8 に修正、冒頭の HTTP 経路の注記を「deprecated 2026-06-22」に更新。
- `docs/rfid-ccid-firmware-checklist.md` — 冒頭に「v2 確定: slot 数 = 13」追記、末尾に「v2 計画書
  よくある落とし穴」節を追加。
- `CHANGELOG.md` — Unreleased に「Docs (RFID 計画書 v2 反映, 2026-06-22)」エントリ。

## Expected Behavior

- docs を読んだ人が「席 1..8 / 13 リーダー / HTTP は新規使用不可 / GPIO は v2 値が正」を一意に
  読み取れる。
- ADR-0015 / ADR-0034 は v1 の判断本文を rewrite せず、v2 追記節として additive に確定事項を残す
  （traceability 規約 §4）。
- コード・config・テストは無変更で `pytest` も無関係に green のまま。

## Implemented Behavior

Expected と一致。docs 6 ファイル + 本 worklog の追加のみ。コード/config/テストは無変更。

## Test Results

- `pytest tests/ -v --ignore=tests/test_vision.py` — 未実行（docs-only。CI / 別タスクでの実行に委ねる）。
- 目視レビュー: v2 計画書 §1〜§3 / §8 と CLAUDE.md / ADR 追記節 / contract / checklist の整合確認済。

## Mismatches Found During Testing

None observed (docs-only; behavior unchanged).

## Fixes Applied

N/A.

## Remaining Gaps / Out-of-Scope

- [ ] **コード/config 側の seat 1..8 化**: `core/session_repository.py` `_MAX_SEAT_NO`,
      `audio/recognizer.py:70`, `tools/probe_pcsc.py:136`, `tools/simulate_rfid.py:155`, `main.py:24`,
      `config_default.json` の `readers` / `pcsc_readers` から `seat_9` を除去。
- [ ] **`config_default.json` の `pcsc_readers` を 13 entry サンプル**（seat_1..8 + board_1..5）に拡張。
- [ ] **HTTP receiver コードの最終処遇**: v2 で deprecated 宣言した `rfid/http_receiver.py` /
      `tools/simulate_rfid.py` の残置期限を ADR で再評価。
- [ ] **実機 E2E**: BUSY timeout デバッグ + 13台密接配置のリーダー間干渉テスト + 必要ならフェライト
      シート対策（v2 計画書 §6 / §7）。
- [ ] **`firmware/esp32s3-pn5180-ccid/README.md`** の更新（13 リーダー前提・確定 GPIO 表の参照）は
      firmware 担当タスクとして残す。

## Related ADRs

- `docs/adr/0015-pn5180-esp32s3-usb-ccid-pcsc-canonical.md` — 本タスクで v2 追記節を追加。
- `docs/adr/0034-rfid-usb-ccid-firmware-host-contract-freeze.md` — 本タスクで v2 追記節を追加。

## Related Issues

- `docs/issues/0015-...` — 既に Fixed。本タスクはその後の v2 計画書反映。

## Related Commits

- (this commit) — RFID 計画書 v2 を docs に反映。
