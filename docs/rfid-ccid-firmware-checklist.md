# ESP32-S3 + PN5180 USB CCID firmware 実装チェックリスト（本番 RFID）

ESP32-S3（PN5180 ×N）firmware を **USB CCID smart card reader** として host PC に公開し、
本アプリ（hand logger）が PC/SC 経由で UID を読めるようにするための **firmware 実装者向け**チェックリスト。

- **正準（normative）は契約** `docs/contracts/rfid-usb-ccid.md` **v1.0**（ADR-0015/0034）。本書はそれを
  firmware 実装の手順に落とした **implementer's guide**。MUST/SHOULD の意味は契約に従う（食い違いは契約優先）。
- **ホスト側（Python / pyscard）は実装・テスト済み**（`rfid/bridge.py` の Get UID、`rfid/reader_thread.py` の
  polling/debounce、`rfid/card_master.py` の UID 正規化、回帰 `tests/test_rfid.py`）。**本チェックリストを
  満たせば host 無改修で `probe_pcsc` と hand logger が通る**。
- 各項目に **受け入れ確認**（`tools/probe_pcsc.py` の出力）を併記。詳細手順は `docs/hardware-qa-checklist.md`。

> 現状（2026-06）: 実機にはテスト用の「カード読み取りで LED 点灯」firmware のみ。PC へは何も送らないため
> host からは見えない。本チェックリストは、その先の **本番 USB CCID firmware** を実装するための仕様。

---

## 0. 大前提（ここを外すと一生 host に見えない）

- [ ] **native USB を使う**。ESP32-S3 の **USB-OTG（native USB）**で USB device を実装する。
      **UART ブリッジ（CP2102N / CH340 等）経由では CCID にできない**（あれは COM ポート＝CDC で、PC/SC に
      乗らない）。配線・基板の USB D±／コネクタを native USB 側にする。
- [ ] **USB CCID class で公開**する（後述 §1）。HID / CDC（シリアル）/ vendor-specific では公開しない。
      CDC で UID を print する設計は **本経路では不可**（PC/SC が認識しない）。
- [ ] 実装は **TinyUSB の CCID class**（または同等の USB CCID 実装）を土台にするのが現実的。

**受け入れ**: Windows「デバイスマネージャー → スマートカード読み取り装置」に出る。
`python tools/probe_pcsc.py list` に reader_name が slot 数だけ並ぶ（CDC=COMポートには出ない）。

## 1. USB descriptors（契約 §2）

- [ ] interface に **`bInterfaceClass = 0x0B`（Smart Card / CCID）** + CCID functional descriptor を付ける。
      Windows 標準の `usbccid` ドライバがバインドする形にする。
- [ ] **VID/PID を固定**する（製作時に確定）。テスト用に実 VID が無くても PID は固定。
- [ ] **manufacturer / product 文字列を固定**。`product` は host の reader_name に現れ config の照合対象に
      なるため、**ファーム更新でも変えない**。
      - 推奨: `manufacturer = "PokerRFID"`, `product = "PN5180-CCID"`（OS が slot 接尾辞を付与 → §2）。
- [ ] **serial 文字列**は device 単位で安定（複数台運用の識別、SHOULD）。

**受け入れ**: `probe_pcsc list` の reader_name に product 文字列（例 `PN5180-CCID [Interface 0]`）が出る。
**確定したら**: 実 **VID/PID** と **実 reader_name** を契約 `rfid-usb-ccid.md` §2/§4 に追記（ISSUE-0015 残作業）。

## 2. CCID multi-slot と reader_name（契約 §3）

- [ ] **PN5180 1 個 = CCID 1 slot**。slot 数 N を host の `pcsc_readers` 件数と一致させる。
- [ ] 各 slot の **reader_name は slot ごとに一意**で、**再列挙・再起動を跨いで安定部分（product + slot index）が
      不変**。OS は `<product> [<iface/slot>] …` の体裁で描画（OS 依存）。
- [ ] **slot 順序を firmware 内で固定**（slot 0,1,2… が常に同じ物理リーダー）。
- [ ] **役割（seat/board）は firmware で決めない**。reader_name↔役割の対応は **host config（`pcsc_readers`）が
      唯一の source of truth**。firmware は slot 順序の安定だけを保証する。

**受け入れ**: `probe_pcsc list` の reader 件数 = slot 数。再起動して名前が変わらない。
host 側は `config.rfid.pcsc_readers[].name` に実 reader_name を**等値**で記入（前方一致しない）。

## 3. ATR（契約 §5）

- [ ] 各 slot は **PC/SC 互換の ATR** を返す（ISO 15693 等の非接触カードは PC/SC v2.01 Part 3 の
      **storage-card proxy ATR** 互換、または vendor ATR）。これで OS の `SCardConnect` が成功する。
- [ ] ATR は card-type ごとに **安定**（同一カード種別で毎回同じ, SHOULD）。
- [ ] **host は ATR の中身を解釈しない**（forward-compat）。connect さえ成立すればよい。凝らなくてよい。
- [ ] **IccPowerOn には常に ATR を返す**（物理カード無しでも）。Windows は bind 直後に IccPowerOn ×3 を送り、
      `ICC_MUTE` を返すと再列挙まで「無応答（0x80100066）」を latch する（ADR-0040）。
- [ ] `Parameters` 応答は `bProtocolNum` と整合させる（T=1 は 7 byte / T=0 は 5 byte）。
- [ ] ATR 受理後に Windows が送る探索 APDU（`00 A4 04 00 …` / `00 CA 7F 68 00`）には `6D 00` でよい。

**受け入れ**: `probe_pcsc raw` で `[OS状態] PRESENT`（`MUTE` が付かない）+ `[connect] connect OK ATR=…`
（`check` は reader 名の確認のみで ATR は検査しない）。

## 4. Get UID pseudo-APDU（契約 §6 — host が依存する唯一の APDU）

- [ ] host は **`FF CA 00 00 00`**（PC/SC Get Data: UID）を送る。これに対し slot は
      **`<UID バイト列> + SW(90 00)`** を返す。
- [ ] **カード不在・読み取り失敗時**は `90 00` 以外（例 `6A 81` / `63 00`）を返す。
      host は非 `90 00` を「UID なし（None）」として扱う（`rfid/bridge.py`: `if (sw1,sw2)!=(0x90,0x00): return None`）。
- [ ] v1.0 で host が要求するのは **Get UID のみ**。ATS/historical（`FF CA 01 00 00`）等は不要（将来 additive）。

**受け入れ**: `probe_pcsc watch` 実行中にカードをかざすと、その slot の行に UID が表示される。

## 5. UID 長と正規化（契約 §7）

- [ ] **UID は生バイトで返す**（4 / 7 / **8** バイト。8B = ISO 15693）。
      **ASCII 整形やコロン挿入を firmware でしない** — host が `bytes_to_tag_id` で
      大文字コロン区切り（例 `04:AB:CD:EF:12:34:56:78`）に正規化する。
- [ ] PN5180 が返す UID の **バイト順**を確認（必要なら firmware で正す）。host の `rfid_cards.json` 登録と
      同じ並びになっていること（登録時に `probe_pcsc watch` の表示 UID をそのまま使えば一致する）。

**受け入れ**: `probe_pcsc watch` の表示が `(8B)` 等で、`⚠ 非契約長` が出ない。登録済みカードは card 名が出る。

## 6. card present/removed・hot-plug・切断（契約 §8）

- [ ] CCID の slot 状態は **常時 present** でよい（推奨・Windows では必須, ADR-0040）。カード有無は
      **Get UID の SW だけ**で伝え、カード無しで `90 00`+UID を返さないこと
      （host は UID の有無で検出。デバウンスは host 側 = 同一 UID 連続は 1 回、外す→再タップで再発火）。
- [ ] **interrupt-IN endpoint は載せない**（Windows が `NotifySlotChange` を無視した実績。host は polling）。
      EP 構成を変えたら `bcdDevice` を上げる（Windows の記述子キャッシュ）。
- [ ] **USB 再列挙 / replug** で reader_name の安定部分が変わらないこと。
- [ ] **切断時**（USB 抜け等）に host がクラッシュしないのは host 側で担保済み（「RFID なしモード」継続）。
      firmware 側は再接続で正しく再列挙できればよい。live hot-add（稼働中の reader 追加追従）は v1.0 対象外。

**受け入れ**: `probe_pcsc watch` で「置く→離す→再度置く」で再発火する。USB 抜き差しで `list` に再度出る。

## 7. やってはいけない / よくある落とし穴

- [ ] UART ブリッジ（CP2102N/CH340）側に挿す・出力する（→ COM ポートになり PC/SC に出ない）。
- [ ] HID / CDC / vendor で公開する（→ CCID として認識されない）。
- [ ] UID を文字列化して返す（→ host 正規化と二重になり不一致）。生バイト + `90 00` が正。
- [ ] product 文字列を版ごとに変える（→ reader_name が動いて config が壊れる）。
- [ ] 役割（seat/board）を reader_name に埋めて host に解釈させる（→ 役割は host config が source of truth）。

---

## 受け入れマトリクス（契約 § ↔ probe_pcsc ↔ 期待）

| 契約 § | 実装項目 | 確認コマンド | 期待 |
|--------|---------|-------------|------|
| §2 | USB CCID class / VID-PID / product | `probe_pcsc list` | reader_name に product、件数 = slot 数 |
| §3-4 | slot↔reader_name 安定・役割は host | `probe_pcsc list`（再起動） | matched、名前不変 |
| §5 | ATR で connect 成立（power-on 常時成功） | `probe_pcsc raw` | `[OS状態] PRESENT`（MUTE 無し）+ `connect OK ATR=…` |
| §6 | Get UID `FF CA 00 00 00`→UID+9000 | `probe_pcsc raw` → `watch` | raw: カード無し `SW=6A81`／置いて `SW=9000`+UID。watch: タップで UID 表示 |
| §7 | UID 4/7/8B 生バイト（MSB-first） | `probe_pcsc watch` | `(8B)`、先頭 `E0:04`（ICODE）、`⚠` 無し、card 解決 |
| §8 | present/removed・hot-plug（SW で伝達） | `probe_pcsc watch` / 抜き差し | 再タップで再発火、再列挙で復帰 |

全項目 PASS → `python main.py --cli`（`rfid.transport="pcsc"`）で board 3/4/5 枚の street 自動遷移まで
確認（`docs/hardware-qa-checklist.md` 手順5）。これで本番 USB CCID 経路の bring-up 完了。

## 完了後に host 側でやること（firmware 確定値の取り込み）

1. `probe_pcsc list` の実 reader_name を `config.json` の `rfid.pcsc_readers[].name` に等値で記入し、
   `transport` を `"pcsc"` にする。
2. 確定した **VID/PID・実 reader_name** を契約 `docs/contracts/rfid-usb-ccid.md` §2/§4 に追記（ISSUE-0015）。
3. 物理カードを `probe_pcsc watch` で読み、表示 UID を `rfid_cards.json`（tag_id→card）に登録。

## 関連

- 契約: `docs/contracts/rfid-usb-ccid.md` v1.0（normative） / ADR-0015 / ADR-0034 / ISSUE-0015
- host 実装: `rfid/bridge.py`（Get UID `FF CA 00 00 00` / SW 90 00 / UID 正規化）/ `rfid/reader_thread.py`
  （slot polling / debounce / board_index）/ `rfid/card_master.py`（`normalize_tag_id` / `bytes_to_tag_id`）
- 診断/手順: `tools/probe_pcsc.py` / `docs/hardware-qa-checklist.md`
