# ESP32-S3 + PN5180 USB CCID firmware 実装チェックリスト（本番 RFID）

ESP32-S3（PN5180 ×N）firmware を **USB CCID smart card reader** として host PC に公開し、
本アプリ（hand logger）が PC/SC 経由で UID を読めるようにするための **firmware 実装者向け**チェックリスト。

- **正準（normative）は契約** `docs/contracts/rfid-usb-ccid.md` **v1.0**（ADR-0015/0034）。本書はそれを
  firmware 実装の手順に落とした **implementer's guide**。MUST/SHOULD の意味は契約に従う（食い違いは契約優先）。
- **ホスト側（Python / pyscard）は実装・テスト済み**（`rfid/bridge.py` の Get UID、`rfid/reader_thread.py` の
  polling/debounce、`rfid/card_master.py` の UID 正規化、回帰 `tests/test_rfid.py`）。**本チェックリストを
  満たせば host 無改修で `probe_pcsc` と hand logger が通る**。
- 各項目に **受け入れ確認**（`tools/probe_pcsc.py` の出力）を併記。詳細手順は `docs/hardware-qa-checklist.md`。
- **v2 計画書 (2026-06-22) 確定**: production の **slot 数 = 13**（席 1..8 = 8 + ボード 1..5 = 5）。
  本書のサンプル `N` はこの 13 を想定する。

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

**受け入れ**: `probe_pcsc check` で当該 slot が `✓ PASS`（connect 成立）。

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
- [ ] **UID は MSB-first（上位バイトを先頭）で返す MUST** （契約 v1.1 §7）。PN5180 / ICODE SLIX の
      生 INVENTORY レスポンスは **LSB-first** で返る実装が一般的なので、firmware で **reverse して
      MSB-first にする**。これを揃えないと、`probe_pcsc watch` の表示と `rfid_cards.json` 登録の
      バイト順が逆転して照合が外れる。host 側は受け取った順をそのまま hex 化するだけ
      （`rfid/bridge.py:bytes_to_tag_id`）。
- [ ] host の `rfid_cards.json` 登録と同じ並びになっていること（登録時に `probe_pcsc watch` の表示
      UID をそのまま使えば一致する）。

**受け入れ**: `probe_pcsc watch` の表示が `(8B)` 等で、`⚠ 非契約長` が出ない。登録済みカードは card 名が出る。

## 6. card present/removed・hot-plug・切断（契約 §8）

- [ ] CCID の **card-present 状態を正しく報告**する。カード無しで Get UID が `90 00`+UID を返さないこと
      （host は UID の有無で検出。デバウンスは host 側 = 同一 UID 連続は 1 回、外す→再タップで再発火）。
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

## 8. RF 時分割スキャン（PN5180 multi-reader 必須）

PN5180 ×13 を 1 基板に集約すると、**複数台が同時に RF ON すると相互干渉**してカード検出ができなく
なる。物理仕様の詳細は `docs/hardware/pn5180-esp32s3-wiring.md` §5、本節は firmware 実装観点の MUST。

- [ ] **同時に RF ON するのは常に 1 台のみ MUST**。複数 PN5180 を並行してフィールド励起してはいけない。
- [ ] CCID slot からの読み取り要求は **時分割で順番に処理** MUST。1 ループあたりの目安:
      MUX 切替 → NSS LOW → LOAD_RF_CONFIG → RF_ON → INVENTORY → RF_OFF → NSS HIGH（5〜10 ms/台）。
- [ ] **SPI clock ≤ 5 MHz** MUST（7 MHz 以上で PN5180 の挙動が不安定になる実測例あり）。Mode 0、MSB first。
- [ ] BUSY 取得時は **MUX 切替 → 数 μs 待機 → SIG 読み** MUST（settling 不足だと前 slot の残値を拾う）。
- [ ] PN5180 BUSY は **High=Busy / Low=Ready**（PN532 と逆論理）。読み方を間違えると永久 timeout する。
- [ ] 13 台 1 サイクルの目安は 65〜130 ms。CCID PC_to_RDR_IccPowerOn のタイムアウトに収まる範囲で
      slot 間スケジューリングする。

**受け入れ**: `probe_pcsc watch` 実行中、複数 slot を順次タップしても全て正しく UID が出る。
「他 slot にカードを置いている間、自 slot が読めなくなる」事象が出ないこと。

> **host 側 polling は意図的に single-threaded sequential**（`rfid/reader_thread.py` の単一スレッドが
> 13 slot を順番に `Get UID`）。時分割は firmware-internal の責務で、host は CCID transfer を
> 順番に出すだけでよい。**将来 host を slot 並列化で高速化しない**こと: OS PC/SC（pcscd/WinSCard）は
> CCID transfer を設計上 serialize するため並列化しても firmware への到達順は変わらず、無駄な
> connect/disconnect と firmware の BUSY 待ちプレッシャーを増やすだけになる。

## 9. トラブルシューティング（実機 bring-up 用）

bring-up 段階の典型症状と切り分け手順。物理層の真実は `docs/hardware/pn5180-esp32s3-wiring.md`、
特に GPIO 表（§3）と変更履歴（§7）を必ず突き合わせる。

### 9.1 `probe_pcsc list` で 0 件

- USB descriptor が **CCID class (`bInterfaceClass=0x0B`)** になっていない（HID / CDC / vendor は不可）。
- ESP32-S3 の **native USB-OTG ポート以外**（UART ブリッジ側 = CP2102N/CH340）に USB ケーブルを
  挿していないか。COM ポート（CDC）に出ているなら PC/SC からは見えない。
- OS デバイスマネージャ（Windows）／`pcscd -fd`（Linux）でデバイス列挙を確認。

### 9.2 `probe_pcsc list` の件数が slot 数より少ない

- firmware の slot 数定義（`CCID_SLOT_COUNT`）と OS が見ている slot 数が不一致。
- USB string descriptor の長さ超過で OS が一部 slot を弾いている可能性。

### 9.3 `probe_pcsc check` で個別 slot が FAIL（ATR 返らず）

- 時分割スキャンの **BUSY 待ちが終わらず**、CCID `PC_to_RDR_IccPowerOn` にタイムアウト応答できていない。
- まず `app_config.h` の **MUX_SIG=GPIO 47** / **MUX_S0=GPIO 37** が反映されているか確認:
  - 旧設計値 **MUX_SIG=21**（strapping pin によるフローティング問題）/ **MUX_S0=38**（NeoPixel 衝突）が
    残っていないかコード grep。
  - 配線図・実装で GPIO 47/37 にジャンパが行っているか実機確認。
- BUSY の論理を **High=Busy / Low=Ready** で扱えているか（PN532 と逆）。
- NSS タイミング / SPI clock（≤5 MHz）を再確認。

### 9.4 `probe_pcsc watch` で UID が出ない / `(0B)` 表示（UID 0 バイト）

- PN5180 から空 UID が返っている = **BUSY を正しく待てていない**可能性が高い。§9.3 の手順を再度。
- INVENTORY コマンドのレスポンス取得タイミング、SPI mode、NSS HIGH→LOW のセットアップ時間を確認。

### 9.5 `probe_pcsc check` で reader_name が見つからない

- `config.rfid.pcsc_readers[].name` の文字列が OS の実 reader_name と **等値**でない（前方一致しない）。
- **前後空白 / 全角空白**が混入していないか（コピペ時の典型）。`probe_pcsc list` の出力をそのまま貼る。
- OS で reader_name 体裁が異なる（Windows: `PokerRFID PN5180-CCID 0` / Linux pcscd: `PokerRFID PN5180-CCID 00 00`
  系 / macOS: 別体裁）。運用 OS の実値で config を書き換える。

### 9.6 既知の GPIO 落とし穴（ESP32-S3 DevKitC-1 v1.0）

- **GPIO 19 / 20**: native USB D-/D+。USB-OTG 使用時は他用途禁止。
- **GPIO 26-32**: 内蔵 PSRAM / Flash 接続。使用禁止。
- **GPIO 21**: strapping pin（起動時フローティング）。MUX SIG には不可 → **47** に移行済。
- **GPIO 38**: DevKitC-1 v1.0 の NeoPixel (WS2812) 専用 → **37** に移行済。

詳細は `docs/hardware/pn5180-esp32s3-wiring.md` §3.4 / §7（変更履歴）参照。

---

## 受け入れマトリクス（契約 § ↔ probe_pcsc ↔ 期待）

| 契約 § | 実装項目 | 確認コマンド | 期待 |
|--------|---------|-------------|------|
| §2 | USB CCID class / VID-PID / product | `probe_pcsc list` | reader_name に product、件数 = slot 数 |
| §3-4 | slot↔reader_name 安定・役割は host | `probe_pcsc list`（再起動） | matched、名前不変 |
| §5 | ATR で connect 成立 | `probe_pcsc check` | 各 slot `✓ PASS` |
| §6 | Get UID `FF CA 00 00 00`→UID+9000 | `probe_pcsc watch` | タップで UID 表示 |
| §7 | UID 4/7/8B 生バイト | `probe_pcsc watch` | `(8B)`、`⚠` 無し、card 解決 |
| §8 | present/removed・hot-plug | `probe_pcsc watch` / 抜き差し | 再タップで再発火、再列挙で復帰 |

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
- 物理層: `docs/hardware/pn5180-esp32s3-wiring.md`（GPIO 表 / 変更履歴 / RF 時分割 / 電源 / コネクタ pinout）
