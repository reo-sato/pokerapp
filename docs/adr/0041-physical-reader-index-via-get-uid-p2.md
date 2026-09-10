# ADR-0041: CCID slot は 1 つに固定し、物理リーダーは Get UID の P2 で選ぶ

## Status

Accepted

## Date

2026-09-10

## Context

本番構成は PN5180 を **11 台**（席 8 + board 3, 契約 v1.1 §3）繋ぐ。契約 v1.0/1.1 はこれを
**CCID の 11 slot**（= PC/SC の reader_name 11 個）として公開する設計だった（ADR-0034 §3）。
2026-09-10 の実機で、この設計が **本番 OS（Windows）で成立しない**ことが確定した（ISSUE-0022）:

- firmware を `CCID_SLOT_COUNT=2`（`bMaxSlotIndex=1`）にして接続しても、PC/SC には
  `PokerRFID PN5180-CCID 0` **1 個しか現れない**。`probe_pcsc list` に `… 1` は出ず、
  config に書くと `Reader not found`。
- 裏付け（既知の制限）:
  - SpringCard TechZone: *"Microsoft's generic CCID driver supports single-slot readers only and
    doesn't even show the other slots"*（Windows の汎用 usbccid は 1 slot のみ）。
  - Microsoft Q&A: SEC1210 の dual-slot reader で 2 つ目の slot が pyscard から見えない、という
    同一症状の報告。
- 回避策として **slot ごとに USB インターフェースを分ける**（composite device で CCID を N 個）方法が
  あるが、ESP32-S3 の USB device controller は **endpoint が 6 本（双方向 5 + IN 1）** しかなく、
  CCID 1 インターフェース = bulk IN/OUT 2 本（ADR-0040 で interrupt-IN は不使用）なので
  **最大 5 台**。本番 11 台に届かない。

一方 host は ADR-0040 の結果として **slot 状態機械に依存しない polling 設計**であり、
UID 取得は Get UID pseudo-APDU（`FF CA 00 00 00`）1 本だけに依存している（契約 §6）。
つまり「どの物理リーダーを読むか」を APDU の中で指定できれば、CCID の slot 多重化は不要になる。

## Decision

**USB 上の CCID slot は常に 1 つ**（`bMaxSlotIndex=0`, reader_name も 1 個）とし、
**物理リーダー k は Get UID pseudo-APDU の P2 で選ぶ**（契約 v1.2 §3/§6）。

- host は `FF CA 00 <k> 00`（k = 0..N-1）を送る。firmware はリーダー k に載っている
  UID（複数枚は 8B 連結 = v1.1 §6 と同じ）+ `90 00` を返す。カード無しは `6A 81`、
  **k が範囲外なら `6A 86`**。`k=0` は従来の `FF CA 00 00 00` と同一（v1.0/1.1 後方互換）。
- **台数問い合わせ**: `FF CA 00 FF 00` → `<N>`（1 byte）+ `90 00`。P2=0xFF は予約。
  v1.1 以前の firmware は非対応（`6A 81` / `6D 00`）なので、host は None として扱い機能を落とさない。
- host config `rfid.pcsc_readers[]` に **`reader`（任意・既定 0）** を追加する。11 台の要素は
  **すべて同じ `name`**（唯一の reader_name）で `reader` が 0..10。一意性の単位は
  `name` から **`(name, reader)`** に変わる。
- host は **reader_name ごとに PC/SC 接続を 1 本だけ持続**させ、そこに N 個の Get UID を流す
  （ADR-0040 で slot は常時 present なので接続は切れない）。transmit が失敗したら接続を捨てて
  次の poll で張り直す。

## Alternatives Considered

- **A. CCID multi-slot（v1.0/1.1 の設計を維持）** — 1 device の N slot を N 個の reader_name として公開。
  - Pros: 契約 v1.1 のまま。host は無改修。役割と slot が 1:1 で分かりやすい。
  - Cons: **Windows の汎用 CCID ドライバが 1 slot しか公開しない**（実機で確定 + 上記 2 出典）。
  - Why rejected: 本番 OS で動かない。ベンダードライバを配らない限り解決しない。
- **B. USB composite device で CCID インターフェースを複数持つ** — slot ごとに独立した CCID interface。
  - Pros: Windows でも各 interface が別 reader として見える（single-slot 制限を回避）。
  - Cons: ESP32-S3 の USB endpoint は 6 本しかなく、CCID 1 個につき bulk 2 本 = **最大 5 台**。
    11 台に届かず、descriptor も肥大する。
  - Why rejected: 台数要件（11）を満たせない。
- **C. ESP32-S3 を複数台（例 3 枚）繋ぐ** — 5 台 ×2〜3 で 11 台を賄う。
  - Pros: B の延長で Windows でも動く。
  - Cons: 基板 3 枚 + USB ハブ + ハーネス再設計、電源・GND・SPI の取り回しが増える。
    reader_name も device ごとに増えて config が複雑化。コストと故障点が増える。
  - Why rejected: firmware 1 行の方針変更（P2 で選ぶ）で済むものにハードウェアを足さない。
- **D. ベンダー製 CCID ドライバ（WinUSB + 自前 PC/SC provider 等）を配布**
  - Pros: multi-slot をそのまま実現できる。
  - Cons: 署名・配布・OS 更新追従の保守負担。店舗 PC への導入ハードル。可搬性を失う。
  - Why rejected: 運用（小規模クラブ）に対して重すぎる。
- **E. host を `SCardControl`（escape）に変える** — PC/SC の APDU 経路を使わず vendor command で読む。
  - Pros: slot 概念から完全に自由。
  - Cons: 契約 §6（host は Get UID pseudo-APDU のみ）を破り、可搬性を失う（ADR-0040 の C と同じ理由）。
  - Why rejected: P2 を使えば標準 APDU の枠内で解決できる。

## Consequences

- Positive:
  - **11 台が Windows で成立する**（台数は USB 記述子ではなく APDU の P2 で決まるので、
    5 台の壁も 13 slot の壁も無い。上限は P2 の 0..254）。
  - **host が速くなる**: 接続を張り直さず 1 本で N 個の APDU を流すため、poll ごとの
    `SCardConnect/IccPowerOn/Disconnect` の往復（11 台ぶん）が消える。
  - 台数を増減しても **USB 記述子・reader_name が変わらない**ので、config の `name` は不変。
    `FF CA 00 FF 00` で firmware の実台数を host から確認できる（`probe_pcsc list`）。
- Negative / trade-offs:
  - **reader_name が 1 つ**になるため、OS の画面や他アプリからは「1 台のリーダー」に見える。
    どの物理リーダーかは host config（`reader`）だけが知る。診断表示は `[r3]` を添えて補う。
  - 1 slot に全リーダーが相乗りするので、**PC/SC 接続 1 本が詰まると全台が止まる**
    （slot 分離による障害分離が無くなる）。transmit 失敗時の再接続で緩和する。
  - firmware の「CCID slot」と「物理リーダー」という 2 つの概念が分離し、実装者が混同しやすい
    （契約 §3/§6 と firmware checklist に明記）。
- Neutral / new constraints:
  - v1.1 §3 の「slot ごとに一意な reader_name」規約は **廃止**（Linux/pcsc-lite 用の MAY としても
    残さない。本番 OS は Windows で、OS ごとの分岐を持たないため）。
  - config の一意性は `(name, reader)`。`name` の重複は正常な設定になった。
  - P2=0xFF は台数問い合わせに予約。`reader` は 0..254。

## Validation / Follow-up

- [x] host 実装 + 単体テスト（`pytest tests/ -q --ignore=tests/test_vision.py` → 823 passed）。
- [x] 契約 `docs/contracts/rfid-usb-ccid.md` を v1.2 に（§2/§3/§4/§6/§8/§10）。
- [ ] 実機 **2 台**（k=0,1）で `probe_pcsc list` の `physical readers: 2` / `check` の PASS /
      `watch` の `seat 1 [r0]` `seat 2 [r1]` を確認。
- [ ] 実機 **11 台**（席 8 + board 3）で通し QA（`docs/hardware-qa-checklist.md`）。
      poll 1 周（11 台 × Get UID）のレイテンシを計測し、`poll_interval_ms` を決める（ISSUE-0021）。
- [ ] `6A 86`（範囲外 k）の実機確認 = config の `reader` を 1 つ大きくして `check` が FAIL すること。

## Related Files

- `rfid/bridge.py`（`get_uid_apdu` の P2 / 共有持続接続 / `query_reader_count` / `6A86` WARN）
- `rfid/reader_thread.py`（config `reader` → bridge factory、旧 1 引数 factory 互換）
- `tools/probe_pcsc.py`（lint の `(name, reader)` / `list` の台数表示 / `check` の SW 判定 /
  `raw --reader` / `watch` の `[rk]`）
- `tools/register_cards.py`（`--reader` で config 要素を選ぶ）
- `config_default.json`（`pcsc_readers` 11 件・同一 `name`・`reader` 0..10）
- `docs/contracts/rfid-usb-ccid.md` v1.2 / `docs/rfid-ccid-firmware-checklist.md`（firmware 側）

## Related Tests

- `tests/test_rfid.py::TestPCSCBridgeReaderIndex` / `::TestQueryReaderCount` / `::TestCallBridgeFactory`
- `tests/test_rfid.py::TestRFIDThread::test_reader_index_is_passed_to_factory`
- `tests/test_tools_probe_pcsc.py::TestLint` / `::TestCheckVerdict` / `::TestCommandsWithPyscardStubbed`
- `tests/test_tools_register_cards.py::TestSelectReader`

## Related Commits

- （本タスク）host + 契約 v1.2 + ADR/ISSUE/QA docs
- firmware 側（別タスク）: P2 による物理リーダー選択 + 台数問い合わせ

## Supersedes / Superseded by

- Supersedes: —（ADR-0034 の契約 §3「PN5180 1 個 = CCID 1 slot / slot ごとの reader_name」を
  **v1.2 で置き換える**。ADR-0034 自体（契約の freeze と他の MUST）は維持。ADR-0040 の
  「slot 常時 present」は前提として継続）
- Superseded by: —
