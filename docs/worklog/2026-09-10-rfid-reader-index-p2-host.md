# Worklog: 物理リーダーを Get UID の P2 で選ぶ（host + 契約 v1.2）

## Date

2026-09-10

## Scope / Task

Windows の汎用 CCID ドライバが 1 slot しか公開しない（ISSUE-0022）ため、**CCID slot を 1 つに固定し
物理リーダーは Get UID の P2 で選ぶ**設計（ADR-0041 / 契約 v1.2）へ、**host（Python）側と契約・docs**
を移行する。firmware 側（`FF CA 00 <k> 00` の実装・台数問い合わせ）は別タスク。

## Goal

- 物理 11 台（席 8 + board 3）が **reader_name 1 個**の PC/SC 環境で動く。
- config `rfid.pcsc_readers[]` に `reader`（P2, 任意・既定 0）を追加し、`(name, reader)` で一意。
- host は reader_name ごとに **PC/SC 接続を 1 本だけ持続**し、そこに 11 個の Get UID を流す
  （poll ごとの connect/disconnect と `IccPowerOn/Off` の往復を無くす）。
- 1 台構成（`reader` 省略）の挙動は v1.0/1.1 と**完全に同一**（既存 config・既存テストが壊れない）。
- 診断ツールが「どの物理リーダーか」を示し、台数不一致（`6A 86`）を検出できる。

## Changed Files

- `rfid/bridge.py` — `PCSCBridge(reader_name, reader_index=0)`（P2 を APDU に埋める `get_uid_apdu`）+
  **reader_name ごとの共有持続接続**（`_SharedConnection` + 参照カウント + `threading.RLock` で
  transmit 直列化、例外で `invalidate()` → 次回 poll で再接続）+ `probe()`（SW だけ返す診断用）+
  `query_reader_count()`（`FF CA 00 FF 00`）+ `6A 86` の 1 回だけ WARN + 同一失敗ログの抑止 +
  `call_bridge_factory()`（新旧 factory シグネチャ互換）。`MockPCSCBridge` は `reader_index` を受理。
- `rfid/reader_thread.py` — config の `reader`（不正値は WARN して 0）を `bridge_factory` に渡す。
  ready ログに reader index。`reader_id` は従来どおり config の並び順（`reader_{i}`）。
- `tools/probe_pcsc.py` — lint を `(name, reader)` 一意 + `reader` 0..254 に（**name 重複は正常**）/
  `list` に `physical readers: N` と `⚠ reader k は firmware の台数 N を超えている` /
  `check` は connect + Get UID 1 回で SW 判定（`9000`/`6A81`=PASS, `6A86`=FAIL）/
  `raw --reader k`（P2, reader 名は `--name` に移動）/ `watch` のラベルに `[rK]`。
- `tools/register_cards.py` — `--reader` を **config 要素の選択**に（index か `seat 1` / `board 1` /
  `board 1-3` ラベル、既定は先頭要素）。選んだ要素の `name`/`reader` で bridge を作る。
- `config_default.json` — `pcsc_readers` を 11 件・**すべて同じ `name`**・`reader` 0..10 に。コメント更新。
- `main.py` — GUI 経路の `RFIDThread` が `readers`(dict, HTTP 用) を渡していた latent bug を CLI 経路と
  同じ `pcsc_readers`(list) フォールバックに揃えた。
- `tests/test_rfid.py` / `tests/test_tools_probe_pcsc.py` / `tests/test_tools_register_cards.py` — 下記。
- `docs/contracts/rfid-usb-ccid.md` — **v1.2**（§1/§2/§3/§4/§6/§8/§10 + Related）。
- `docs/adr/0041-physical-reader-index-via-get-uid-p2.md` / `docs/issues/0022-windows-ccid-single-slot.md`
  / `docs/decision-log.md`（索引 2 行）/ `docs/hardware-qa-checklist.md`（手順 1/2/3/4/6-7・受け入れ基準）
  / `docs/installation.md` / `CHANGELOG.md` / `CLAUDE.md`。

## Expected Behavior

- **bridge**: `PCSCBridge(name, k)` の Get UID は `FF CA 00 <k> 00`。`k=0` は従来と同一バイト列。
  `reader_index` は 0..254（255 は台数問い合わせ用に予約）で、範囲外・非 int は `ValueError`。
  最初の `read_uids()` で connect し、以後は同じ接続で transmit のみ。例外が出たら接続を破棄して
  次回張り直す（返り値は従来どおり `[]`、ログは DEBUG で同一内容なら 1 回）。`6A 86` は `[]` + WARN 1 回。
  同じ reader_name の bridge 群は接続を 1 本共有し、最後の `close()` で切断。
- **RFIDThread**: config の `reader` を factory に渡す。旧 1 引数 factory も動く。UID 集合デバウンスと
  board offset は reader_id 単位のまま（v1.1 の挙動不変）。
- **probe_pcsc**: `list` が台数 N を表示し config の `reader` が N 以上なら警告。`check` が SW で
  PASS/FAIL。`watch` が `seat 1 [r0]` の形で物理リーダーを示す。`raw --reader k` で 1 台を直接叩ける。
- **register_cards**: `--reader` で config 要素を選び、その `name`/`reader` で登録する。

## Implemented Behavior

上記のとおり実装。差異・判断:

- **接続の共有はモジュールレベルの参照カウント**（`rfid/bridge.py` の `_shared_connections`）。
  RFIDThread は 1 本だが、`transmit` は `RLock` で直列化してある（診断ツールが別スレッドから
  同じ reader を触っても壊れない）。`query_reader_count` も同じ機構を acquire/release して使う。
- **`check` の SW 取得は `bridge.probe()` を持つ bridge のみ**。`probe()` を持たない mock/旧 bridge は
  SW=None として「connect 成立なら PASS」（既存テストの DI を壊さないため）。
- **`raw` の `--reader` は P2 に転用**し、reader 名の指定は `--name` に移した（1 名前しか無いので
  名前指定の必要性が下がった）。既定は `--reader 0` + config 先頭で接続中の名前。
- **`reader_label` の `[rK]` は `reader` キーがあるときだけ付く**（既存の表示・テストを壊さない）。

## Test Results

- `pytest tests/ -q --ignore=tests/test_vision.py` — **823 passed, 0 failed**
  （RFID 系テストは 142 → 191 件 = +49: `test_rfid.py` 66→83 / `test_tools_probe_pcsc.py` 54→77 /
  `test_tools_register_cards.py` 22→31）。
- 追加テスト:
  - `tests/test_rfid.py::TestPCSCBridgeReaderIndex`（P2 が APDU に入る / 既定は v1.0 と同一 /
    `reader_index` の型・範囲 / 接続の持続（2 回目は connect しない）/ 例外後の再接続 /
    reader_name ごとに 1 接続（参照カウント）/ `6A86` は `[]` + WARN 1 回 / `probe()`）。
  - `::TestQueryReaderCount`（fake pyscard を `sys.modules` に差し替え: 台数取得 / 非対応 firmware /
    reader 不在 / 例外）、`::TestCallBridgeFactory`（新旧 factory）。
  - `::TestRFIDThread::test_reader_index_is_passed_to_factory` / `test_legacy_one_arg_factory_still_works`。
  - `tests/test_tools_probe_pcsc.py`: lint（name 重複可 / `(name, reader)` 重複 NG / `reader` 範囲 /
    既定 config の 11 件が lint 緑）、`reader_label` の `[rK]`、`probe_connect` の index 伝播と SW、
    `check_verdict` の 5 分岐、`_cmd_list` の台数表示・超過警告・非対応注記、`_cmd_check` の
    カード無し PASS と `6A86` FAIL、`raw --reader/--name` の parser、`format_event`/`run_watch` の `[rK]`。
  - `tests/test_tools_register_cards.py::TestSelectReader` ほか CLI 経路（選択 / 既定 / 不正 selector）。
- 実機確認は未（1 台の疎通は ADR-0040 で確認済。2 台 →11 台は ADR-0041 の Follow-up）。

## Mismatches Found During Testing

- **`main.py` の GUI 経路が `pcsc_readers` を渡していなかった**（`rfid_cfg.get("readers", [])` =
  HTTP 用 dict）。dict を `RFIDThread` に渡すと `enumerate` が str を返して `cfg.get` で落ちるため、
  `transport="pcsc"` + GUI モードでは RFID が起動しなかった（CLI 経路は ADR-0034 で修正済だった）。
  本タスクの config 変更で顕在化しやすくなるため、その場で CLI と同じフォールバックに揃えた。
- 既存テストの `_connected_bridge` は `createConnection()` で例外を投げる fake だったため、
  「接続の持続」を測れなかった。fake を connect/transmit/disconnect の回数を数える形に作り替え、
  共有接続がテスト間で混ざらないよう **reader_name を一意に採番**するようにした。

## Fixes Applied

- `main.py` GUI 経路の `reader_configs` を `pcsc_readers`（list）フォールバックに統一。
- 失敗ログの連打（100ms poll × 11 台）を避けるため、`PCSCBridge` に「同一内容の失敗は 1 回だけ
  DEBUG」を入れた（`6A 86` は WARN 1 回）。
- `probe_connect` / `register_cards` / `RFIDThread` の factory 呼び出しを `call_bridge_factory` に集約
  （旧 1 引数 factory の互換を 1 か所で担保）。

## Remaining Gaps / Out-of-Scope

- [ ] **firmware 側**（別タスク）: `FF CA 00 <k> 00` の P2 対応 / `6A 86` / `FF CA 00 FF 00` の台数応答 /
      `CCID_SLOT_COUNT` を 1 固定。`docs/rfid-ccid-firmware-checklist.md` §8 への追記も firmware 側。
- [ ] **実機検証**: 2 台（`[r0]`/`[r1]`）→ 11 台の通し QA、poll 1 周のレイテンシ計測（ISSUE-0021 と合流）。
- [ ] live hot-add（稼働中の reader 追加追従）は引き続き未対応（起動時 connect のみ）。
- [ ] `rfid_cards.json` への実カード登録（11 台構成での運用手順は QA チェックリスト手順 3）。

## Related ADRs

- `docs/adr/0041-physical-reader-index-via-get-uid-p2.md` — 本作業の決定。
- `docs/adr/0040-ccid-virtual-card-always-present.md` — slot 常時 present（1 接続持続の前提）。
- `docs/adr/0034-rfid-usb-ccid-firmware-host-contract-freeze.md` — 契約 freeze（§3 を v1.2 で置換）。

## Related Issues

- `docs/issues/0022-windows-ccid-single-slot.md` — 本作業の出所（Windows usbccid の single-slot 制限）。
- `docs/issues/0021-pn5180-poll-cycle-latency.md` — 11 台の poll 周期はこちらと合流。

## Related Commits

- （未コミット）host + 契約 v1.2 + ADR-0041 / ISSUE-0022 / QA docs
