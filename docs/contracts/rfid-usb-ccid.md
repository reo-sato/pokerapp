# RFID USB CCID firmware ↔ host (PC/SC) contract

**version: 1.0 (frozen, ADR-0034)** ／ canonical RFID transport（ADR-0015）の firmware↔Python 境界。

ESP32-S3（PN5180 ×N）firmware と host Python（pyscard / PC/SC, `rfid/bridge.py` /
`rfid/reader_thread.py`）は別々に実装される。drift を防ぐため、host が依存する **USB descriptor /
reader_name / ATR / pseudo-APDU / UID / hot-plug** の境界を本書で固定する（ISSUE-0015）。

HTTP 経路（`rfid/http_receiver.py`, ADR-0015 で optional secondary）は本契約の対象外（debug/remote 用）。

凡例: **MUST** = 準拠必須 / **SHOULD** = 推奨 / **MAY** = 任意。「host」= Python、「firmware」= ESP32-S3。

---

## 1. 役割と全体像

```
[PN5180 ×N] ──(SPI)── [ESP32-S3 native USB = USB CCID class] ──(USB)── [host PC]
                                                                         OS PC/SC stack
                                                                         (pcscd / WinSCard)
                                                                            │ pyscard
                                                                   rfid/bridge.py (PCSCBridge)
                                                                   rfid/reader_thread.py
```

- firmware は PN5180 ×N を **1 つの USB CCID composite device の N slot** として host PC/SC に公開する。
- host は OS 標準 PC/SC スタック越しに **pyscard** で各 slot（reader_name）を列挙・読み取る。WiFi/HTTP 不要。

## 2. USB descriptors（firmware MUST）

- device は **USB CCID class（bInterfaceClass=0x0B, Smart Card）** を実装する **MUST**。HID/vendor-specific
  での RFID 公開はしない（PC/SC スタックに自然に乗せるため）。
- **VID/PID**: 製作時に確定し、確定値を本節に追記する **MUST**（host は VID/PID を直接見ず reader_name で
  マッチするが、衝突回避・ドライバ選択のため固定する）。テスト用途で実 VID を持たない場合も PID は固定。
  - **確定値（2026-06-22, 実機 bring-up）**: `VID=0x303A PID=0x8B5D`（Espressif VID、PID は本プロジェクト固定）。
- **manufacturer / product 文字列**: 固定 **MUST**。product 文字列は host の reader_name に現れ、config の
  マッチ対象になるため **安定**（ファーム更新で変えない）**MUST**。
  - **確定値**: `manufacturer=PokerRFID`、`product=PN5180-CCID`（Windows PC/SC は `<manufacturer> <product> <slot index>` の体裁で描画 → §4 reader_name 参照）。
- **serial 文字列**: device 単位で安定 **SHOULD**（複数台運用時の識別。reader_name に現れうる）。
- **endpoint 構成（2026-09-10 実機で確定, ADR-0040）**: bulk OUT + bulk IN の **2 本のみ**とし、
  interrupt-IN（`RDR_to_PC_NotifySlotChange`）は載せない **SHOULD**。Windows(usbccid) は interrupt-IN が
  あると通知を読み取っても slot 状態に反映せず、無くても `GetSlotStatus` を polling しない（カード有無は
  §8 の方式で伝える）。記述子の EP 構成を変えるときは `bcdDevice` を上げる（Windows は VID/PID/REV で
  記述子をキャッシュする。現在 `0x0102`）。

## 3. CCID multi-slot と reader_name（firmware MUST / host MUST）

- firmware は PN5180 1 個 = CCID **1 slot** として公開する **MUST**。slot 数 N は config と一致させる。
- 各 slot の reader_name は **slot ごとに一意で、再列挙・再起動を跨いで安定** **MUST**。OS が
  `<product> [<iface/slot>] (<serial>) <NN> <MM>` 形式で描画する（OS 依存, §8）が、**安定部分（product 文字列
  + slot index）が変わらない**こと **MUST**。
- **slot 順序**は firmware 内で固定 **MUST**（slot 0,1,2,… が常に同じ物理リーダーに対応）。
  - 推奨マッピング（firmware の slot index → 役割）: `0..(S-1)` = seat 1..S、続く `S..(S+4)` = board 1..5。
    ただし **正準は host config**（§4）であり、firmware は順序の安定のみ保証する。

## 4. reader_name ↔ 役割（host config の正準形）

host は canonical PC/SC 経路で `config.rfid.pcsc_readers` を **list** として解釈する **MUST**:

```jsonc
"rfid": {
  "transport": "pcsc",
  "pcsc_readers": [
    {"name": "PokerRFID PN5180-CCID 0", "role": "seat",  "seat": 1},
    {"name": "PokerRFID PN5180-CCID 1", "role": "seat",  "seat": 2},
    {"name": "PokerRFID PN5180-CCID 5", "role": "board", "index": 1}
  ]
}
```

- 各要素 = `{"name": <PC/SC reader_name 完全一致文字列>, "role": "seat"|"board", "seat": 1..9 (role=seat),
  "index": 1..5 (role=board, 任意)}`。
- `name` は **OS が描画する reader_name と完全一致** **MUST**（host は前方一致でなく等値で照合,
  `rfid/bridge.py:PCSCBridge.connect`）。OS により文字列が異なるため、運用 OS の実値を入れる（§8）。
- **確定値（Windows, 2026-06-22 実機）**: 1 slot 構成で `PokerRFID PN5180-CCID 0`（manufacturer + product + slot index, 半角空白区切り）。
  Linux/macOS の体裁は別なので、運用 OS で `probe_pcsc list` を実行して実 reader_name を確認すること。
- 役割→`RFIDEvent.role`/`.seat` は host config が **唯一の source of truth**。firmware は slot 順序のみ保証。
- HTTP 経路の `config.rfid.readers`（**dict**, `{reader_id: {role,seat}}`）とは **別キー**であり混同しない
  **MUST**（list=pcsc / dict=http）。

## 5. ATR（firmware MUST / host: ATR-agnostic）

- firmware は各 card-type について **PC/SC 互換の ATR** を slot から返す **MUST**（ISO 15693 等の非接触カードは
  PC/SC v2.01 Part 3 の **storage-card proxy ATR** 互換、または vendor ATR）。これにより OS PC/SC が
  `SCardConnect` を成功させ、host の `createConnection().connect()` が通る。
- ATR は card-type ごとに **安定** **SHOULD**（同一カード種別で毎回同じ）。
- **host は UID 読み取りに特定 ATR バイトを前提にしない**（forward-compat）**MUST**。host は connect 成功後に
  §6 の Get UID pseudo-APDU のみで UID を取得する（`rfid/bridge.py` は ATR を解釈しない）。
- **power-on は常に成功させる（firmware MUST, 2026-09-10 追記, ADR-0040）**: Windows(usbccid) は bind 直後に
  `PC_to_RDR_IccPowerOn` を送り、`ICC_MUTE` を返すとカードを「無応答（`0x80100066`）」として latch し
  再列挙まで再試行しない。よって firmware は物理カードの有無に関わらず IccPowerOn に固定 ATR を返す
  （slot は常時 present, §8）。ATR 受理後に OS がカード種別探索の APDU（`00 A4 04 00 …` SELECT AID /
  `00 CA 7F 68 00` GET DATA 等）を送ることがあるが、`6D 00`（INS 未対応）で応答してよい。
  `Parameters` 応答は `bProtocolNum` と整合させる（T=1 は 7 byte / T=0 は 5 byte）。
  - **確定 ATR（実機で受理）**: `3B 8F 80 01 80 4F 0C A0 00 00 03 06 03 00 01 00 00 00 00 6A`
    （PC/SC v2.01 Part 3 storage-card proxy, T=0/T=1, TCK 整合）。

## 6. pseudo-APDU（firmware MUST / host 実装済）

- **Get UID**: host は `FF CA 00 00 00`（PC/SC v2.01 Part 3 Get Data: UID）を送る。firmware/slot は
  **応答に UID バイト列 + SW=`90 00`** を返す **MUST**（`rfid/bridge.py:_GET_UID_APDU` / `_SW_OK`）。
  - カード不在・読み取り失敗時は `90 00` 以外（例 `6A 81` / `63 00`）を返す **SHOULD**（host は非 `90 00` を
    「UID なし」として None 化）。
- v1.0 で host が依存する pseudo-APDU は **Get UID のみ**。ATS/historical bytes（`FF CA 01 00 00`）等は
  **本契約の対象外**（additive に v1.1+ で追加可能）。

## 7. UID 長と正規化（host MUST）

- UID は **4 / 7 / 8 バイト**を取り得る（4=Mifare Classic、7=Type A 7-byte、**8=ISO 15693**）。
- host は UID を **長さ非依存**で扱い、`bytes_to_tag_id` / `normalize_tag_id` で **大文字コロン区切り 16 進**に
  正規化する **MUST**（例: 8B `04 AB CD EF 12 34 56 78` → `04:AB:CD:EF:12:34:56:78`）。`rfid_cards.json` の
  tag_id も同正規化で照合する。

## 8. hot-plug / 再列挙 / multi-platform

- **card present/removed**: host は各 slot を `poll_interval_ms`（既定 100ms）で polling し、UID の有無で
  検出する（`RFIDThread`。同一 UID 連続はデバウンスで 1 回, 外れ→再タッチで再発火）。**カード有無は
  Get UID の SW だけで伝える**（あり: UID + `90 00` / なし: `6A 81` 等）**MUST**。host は CCID の slot 状態
  （bmICCStatus / NotifySlotChange）に依存しないため、firmware は slot を **常時 present** として公開してよい
  （**推奨・Windows では必須**, ADR-0040: 物理有無を slot 状態に反映すると Windows が bind 時に MUTE を
  latch する）。**カード無しで Get UID が `90 00`+UID を返さない**ことが唯一の不変条件 **MUST**。
- **USB 再列挙 / replug**: reader_name の安定部分（§3）が変わらない **MUST**。host の live 再列挙対応
  （実行中の reader 追加・名称変化の追従）は **本 v1.0 では起動時 connect のみ**（live hot-add は future,
  ISSUE 追跡）。
- **rfid 切断時**: USB 切断・CCID 再列挙で connect 不能でも host はクラッシュしない（`rfid.enabled=false`
  相当の「RFID なしモード」で継続）**MUST**（エラーハンドリング方針, CLAUDE.md）。
- **multi-platform**: Linux `pcscd` / macOS / Windows WinSCard で動作する **MUST**。reader_name の描画は OS
  依存（OS が product/slot/serial を異なる体裁で連結）。よって `pcsc_readers[].name` は **運用 OS の実 reader_name**
  を入れる（`python -c "from smartcard.System import readers; print([str(r) for r in readers()])"` で確認）。

## 9. host 不変条件（hardware 非依存・既存契約）

- `RFIDEvent`（`core/events.py`）/ confidence 行列 / reader 役割語彙（`seat_1..9` / `board_1..5`）は
  hardware 非依存で **不変**。本契約が変わっても上流（integration / confidence）は影響を受けない。

## 10. versioning / freeze

- 本契約は **v1.0 frozen**（ADR-0034）。後方互換な追加（新 pseudo-APDU、ATR 種別追加、live hot-add）は
  **minor bump**（1.1, 1.2…）。reader_name 規約・Get UID・UID 正規化の **意味変更は breaking（major）**。
- firmware の確定値（VID/PID、実 reader_name）は確定し次第 §2/§4 に追記する（host コードは変更不要 = 契約安定）。

## Related

- **`docs/rfid-ccid-firmware-checklist.md`** — 本契約の MUST を ESP32-S3 firmware 実装手順に落とした
  implementer's guide（各項目を `tools/probe_pcsc.py` で受け入れ確認）。
- ADR-0015（PC/SC canonical）/ ADR-0034（本契約 freeze）/ ISSUE-0015（本契約の出所）
- `rfid/bridge.py`（Get UID / UID 正規化）/ `rfid/reader_thread.py`（pcsc_readers / polling / debounce）
- `rfid/card_master.py`（`normalize_tag_id` / `bytes_to_tag_id`）/ `tests/test_rfid.py`
