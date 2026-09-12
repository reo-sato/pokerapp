# ADR-0040: CCID slot は仮想カード「常時挿入」とし、カード有無は Get UID の SW だけで伝える

## Status

Accepted

## Date

2026-09-10

## Context

ESP32-S3 + PN5180 を USB CCID smart card reader として Windows PC/SC に公開する firmware
（`firmware/esp32s3-pn5180-ccid/`, 契約 `docs/contracts/rfid-usb-ccid.md`, ADR-0015/0034）の実機
bring-up で、firmware がカードを読めている（`🎴 UID=E0:04:…`）のに host の `probe_pcsc watch` が
0 件のまま、という状態が続いた。`tools/probe_pcsc.py raw`（pyscard 直叩き）と firmware の CCID
コマンドログで、Windows の CCID class driver（`Microsoft Usbccid Smartcard Reader (WUDF)`）の挙動が
次のとおりであることを実機で確定した:

- **interrupt-IN endpoint あり**（`RDR_to_PC_NotifySlotChange` で挿抜通知）: Windows は通知を
  読み取る（次の通知が送れる＝前の通知は消費済み）が、slot 状態に反映しない。
  `SCardGetStatusChange` は 20 秒以上 `EMPTY` のまま、`SCardConnect` は `0x80100069
  SCARD_W_REMOVED_CARD`。firmware には `GetSlotStatus` / `IccPowerOn` が一切届かない。
- **interrupt-IN endpoint なし**（bulk OUT/IN のみ）: Windows は `GetSlotStatus` を **polling しない**。
  bind 直後（`ccid_open` の約 50ms 後）に `IccPowerOn` を 3 回送るだけで、その時 firmware が
  `ICC_MUTE(0xFE)`（cache が absent = poll task 起動前）を返すと **`PRESENT|MUTE` を latch**
  （`0x80100066 SCARD_W_UNRESPONSIVE_CARD`）し、以後カードを置いても再列挙まで何も送らない。

一方 host 側（`rfid/bridge.py:PCSCBridge.read_uid` / `rfid/reader_thread.py:RFIDThread`）は
最初から **polling 設計**である: 100ms ごとに `SCardConnect → FF CA 00 00 00 → SCardDisconnect` を
繰り返し、SW≠`90 00` を「カード無し(None)」と扱い、None↔UID の遷移で debounce と再発火を行う
（契約 §6/§8）。つまり host は CCID の slot 状態（bmICCStatus / NotifySlotChange）に**依存していない**。

物理カードの有無を CCID の slot 状態で「正しく」伝えようとする設計（契約 v1.0 §8 の文言）は、
Windows では成立せず、host にとっても不要だった。

## Decision

firmware は各 CCID slot を **仮想カードが常時挿入された状態**として公開する。

- `GetSlotStatus` は常に present（`IccPowerOn` 前は present&inactive、後は present&active）を返す。
- `IccPowerOn` は物理カードの有無に関わらず **常に固定 ATR** を返す（`ICC_MUTE` を返す経路を持たない）。
  `IccPowerOff` は present&inactive を返す（absent を返すと「抜かれた」扱いになる）。
- 物理カードの有無は **Get UID pseudo-APDU（`FF CA 00 00 00`）の SW だけ**で伝える:
  カードあり → UID + `90 00`、なし → `6A 81`。**カード無しで `90 00` + UID を返さない**ことが唯一の不変条件。
- interrupt-IN endpoint は記述子に **載せない**（bulk OUT/IN の 2 本）。
- ATR 受理後に Windows がカード種別探索の APDU（`00 A4 04 00 …` SELECT AID / `00 CA 7F 68 00`
  GET DATA 等）を送ってくるが、`6D 00`（INS 未対応）で応答する。

実装はコンパイル時スイッチで切り替え可能にする（既定は上記）:
`app_config.h: CCID_VIRTUAL_CARD_ALWAYS_PRESENT=1` / `usb_descriptors.h: CCID_USE_INTERRUPT_EP=0`。
pcsc-lite（Linux）のように polling が信頼できる環境で物理有無を slot 状態に反映したい場合は
`ALWAYS_PRESENT=0` にできる（通知機構のコードも残置）。

## Alternatives Considered

- **A. interrupt-IN endpoint で `NotifySlotChange` を送る（CCID の正攻法）**
  - Pros: 仕様どおり。非同期にカード挿入を host に伝えられる。
  - Cons: Windows(usbccid) が通知を消費しつつ slot 状態に反映しなかった（実機で再現）。原因は
    ドライバ内部で特定できず。host が polling 設計のため非同期通知の利点もない。
  - Why rejected: 実機で機能せず、機能しても host に価値がない。コードは `CCID_USE_INTERRUPT_EP` で残置。
- **B. bulk のみで物理カード有無を `GetSlotStatus` の bmICCStatus に反映する（polling 前提）**
  - Pros: 契約 v1.0 §8 の文言どおり。pcsc-lite では動く見込み。
  - Cons: Windows は polling せず、bind 直後の `IccPowerOn` に `ICC_MUTE` を返すと再列挙まで
    latch する。poll task 起動前や取りこぼし中に power-on が来る競合を firmware 側で完全には防げない。
  - Why rejected: Windows で成立しない。
- **C. host を `SCARD_SHARE_DIRECT` + escape（`SCardControl`）に変える**
  - Pros: OS の slot 状態機械を完全に迂回できる。
  - Cons: PC/SC の可搬性を失い、契約 §6（host は Get UID pseudo-APDU のみ）を破る。host 改修が必要。
  - Why rejected: firmware 側の 1 行の方針変更で済むものを host 契約の変更にしない。

## Consequences

- Positive:
  - Windows で `SCardConnect` が常に成立し、`watch`/`RFIDThread` の polling がそのまま動く。
    OS のスロット状態機械（interrupt/polling/MUTE latch）への依存が消える。
  - `probe_pcsc raw` が **カード無しでも** `PRESENT` + `SW=6A81` を返すため、CCID 経路の疎通を
    カード無しで確認できる。13 slot 構成でも各 slot が同じ規則で振る舞う。
- Negative / trade-offs:
  - OS 上は常に「カード挿入済み」に見える（デバイスマネージャ / 他アプリからの見え方は cosmetic）。
  - pyscard の `disconnect()` 既定（unpower）により、host の poll ごとに `IccPowerOn/Off` が
    firmware に届く。処理は軽いが、CCID コマンドを INFO で全件ログすると UART が詰まりうる
    （13 slot × 10Hz）。bring-up 完了後にログ水準を下げる（Follow-up）。
  - 契約 v1.0 §8 の「firmware は card-present 状態を正しく報告する MUST」は緩和され、
    「カード無しで `90 00`+UID を返さない」に置き換わる（additive: host は無改修）。
- Neutral / new constraints:
  - 記述子の endpoint 構成を変えるときは `bcdDevice` を上げる（Windows は VID/PID/REV で記述子を
    キャッシュする。2 EP=0x0100 → +interrupt=0x0101 → 既定 OFF=0x0102）。
  - Get UID の SW が唯一の presence チャネルなので、firmware 側の presence 保持（debounce）が
    host の再発火粒度を決める（`PRESENCE_HOLD_MISSES`）。

## Validation / Follow-up

- [x] 実機（2026-09-10, Windows / reader #8）: `probe_pcsc raw` で `[OS状態] PRESENT`（MUTE 無し）、
      カード無し `SW=6A81` → 置いて `Get UID: E0 04 01 53 1A 41 19 75 SW=9000` → 離して `6A81`。
      firmware ログ: `IccPowerOn → ATR 返却` → `SetParameters T=1 (7B)` → 探索 APDU に `6D 00`。
- [x] `probe_pcsc watch --seconds 30`（本番 `RFIDThread`）で `seat 1 UID=E0:04:01:53:1C:2A:B2:6C (8B)` が
      「置く→離す→置く」で **2 回**出た（2026-09-10 実機, §8 再発火 OK）。
- [x] CCID コマンドログ（IccPowerOn/Off, XfrBlock, Parameters）を DEBUG に（初回 IccPowerOn のみ INFO）。
      jef-sure ドライバのタグ（`Tag Found!` 等）は `esp_log_level_set` で WARN 以上に（13 slot 時の UART 帯域）。
- [ ] 13 slot 化（`CCID_SLOT_COUNT=13`）で各 slot が常時 present / SW で有無、を確認。
- [ ] 契約 `docs/contracts/rfid-usb-ccid.md` §2/§5/§8 に本決定を additive に反映（本 ADR と同時）。

## Related Files

- `firmware/esp32s3-pn5180-ccid/main/app_config.h`（`CCID_VIRTUAL_CARD_ALWAYS_PRESENT`）
- `firmware/esp32s3-pn5180-ccid/main/ccid_slot.c`（`icc_status` / IccPowerOn / Parameters / NotifySlotChange 組立）
- `firmware/esp32s3-pn5180-ccid/main/usb_descriptors.[ch]`（`CCID_USE_INTERRUPT_EP`, `bcdDevice`）
- `firmware/esp32s3-pn5180-ccid/main/ccid_device.c`（EP open / interrupt 送出）
- `rfid/bridge.py` / `rfid/reader_thread.py`（host 側 polling / SW 判定 / debounce — 無改修）
- `tools/probe_pcsc.py`（`raw` サブコマンド）

## Related Tests

- `tests/test_tools_probe_pcsc.py::TestRawHelpers`（`raw` の純粋ヘルパ / parser 登録）
- 実機受け入れ: `docs/rfid-ccid-firmware-checklist.md` §3/§6 の受け入れ手順

## Related Commits

- `af0b092` — interrupt-IN + NotifySlotChange（試行）
- `5bcbf9a` — interrupt-IN を既定 OFF（Windows が通知を無視）
- `9846c13` — Parameters を T=1 の 7 byte に / MUTE 経路の可視化
- `7091e3c` — 仮想カード常時挿入（本決定）
- `f69ce60` — `probe_pcsc raw`

## Supersedes / Superseded by

- Supersedes: —（ADR-0034 の契約 §8 文言を additive に緩和。ADR-0034 自体は維持）
- Superseded by: —
