# ESP32-S3 + PN5180 USB CCID firmware 実装チェックリスト（本番 RFID）

ESP32-S3（PN5180 ×N）firmware を **USB CCID smart card reader** として host PC に公開し、
本アプリ（hand logger）が PC/SC 経由で UID を読めるようにするための **firmware 実装者向け**チェックリスト。

- **正準（normative）は契約** `docs/contracts/rfid-usb-ccid.md` **v1.2**（ADR-0015/0034/0041）。本書はそれを
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
`python tools/probe_pcsc.py list` に reader_name が **1 件**出る（CDC=COMポートには出ない）。
物理リーダーの台数は `physical readers: N` で確認する（§2/§4）。

## 1. USB descriptors（契約 §2）

- [ ] interface に **`bInterfaceClass = 0x0B`（Smart Card / CCID）** + CCID functional descriptor を付ける。
      Windows 標準の `usbccid` ドライバがバインドする形にする。
- [ ] **VID/PID を固定**する（製作時に確定）。テスト用に実 VID が無くても PID は固定。
- [ ] **manufacturer / product 文字列を固定**。`product` は host の reader_name に現れ config の照合対象に
      なるため、**ファーム更新でも変えない**。
      - 実機確定値: `manufacturer = "PokerRFID"`, `product = "PN5180-CCID"`（OS が slot 接尾辞 ` 0` を付与
        → Windows の reader_name は `PokerRFID PN5180-CCID 0` の 1 件だけ。§2）。
- [ ] **serial 文字列**は device 単位で安定（複数台運用の識別、SHOULD）。

**受け入れ**: `probe_pcsc list` の reader_name に product 文字列（例 `PN5180-CCID [Interface 0]`）が出る。
**確定したら**: 実 **VID/PID** と **実 reader_name** を契約 `rfid-usb-ccid.md` §2/§4 に追記（ISSUE-0015 残作業）。

## 2. CCID slot は 1 つ / 物理リーダーは Get UID の P2（契約 v1.2 §3, ADR-0041）

- [ ] **CCID slot は 1 つだけ**（`bMaxSlotIndex = 0`）。**CCID multi-slot は使わない**。
      Windows の汎用 CCID ドライバ（usbccid）は **1 インターフェースにつき 1 slot しか reader として
      公開しない**（実機 2026-09-10: 2 slot で焼いても `PokerRFID PN5180-CCID 1` は `Reader not found`）。
      slot ごとに USB インターフェースを分ける回避策も、ESP32-S3 の USB endpoint が 6 本
      （双方向 5 + IN 1）なので **最大 5 台**にしかならず本番 11 台に届かない。
- [ ] **物理リーダーは Get UID の P2（reader index k）で選ぶ**（§4）。firmware は
      **index 順序を固定**する（index 0,1,2… が常に同じ物理リーダー = 配線表の並び）。
- [ ] **台数 N を `FF CA 00 FF 00` で答える**（host が config の index を検証できるように）。
- [ ] reader_name は **1 つだけ**（`<product> 0` 等）で、再列挙・再起動を跨いで**不変**。
- [ ] **役割（seat/board）は firmware で決めない**。index↔役割の対応は **host config
      （`pcsc_readers[].reader`）が唯一の source of truth**。firmware は index 順序の安定だけを保証する。

**受け入れ**: `probe_pcsc list` の reader 件数は **1**、`physical readers: N` が firmware の台数と一致。
再起動して名前が変わらない。host 側は `config.rfid.pcsc_readers[].name` に実 reader_name を**等値**で、
`reader` に **物理 reader index** を記入する。

## 3. ATR（契約 §5）

- [ ] （唯一の）slot は **PC/SC 互換の ATR** を返す（ISO 15693 等の非接触カードは PC/SC v2.01 Part 3 の
      **storage-card proxy ATR** 互換、または vendor ATR）。これで OS の `SCardConnect` が成功する。
- [ ] ATR は card-type ごとに **安定**（同一カード種別で毎回同じ, SHOULD）。
- [ ] **host は ATR の中身を解釈しない**（forward-compat）。connect さえ成立すればよい。凝らなくてよい。
- [ ] **IccPowerOn には常に ATR を返す**（物理カード無しでも）。Windows は bind 直後に IccPowerOn ×3 を送り、
      `ICC_MUTE` を返すと再列挙まで「無応答（0x80100066）」を latch する（ADR-0040）。
- [ ] `Parameters` 応答は `bProtocolNum` と整合させる（T=1 は 7 byte / T=0 は 5 byte）。
- [ ] ATR 受理後に Windows が送る探索 APDU（`00 A4 04 00 …` / `00 CA 7F 68 00`）には `6D 00` でよい。

**受け入れ**: `probe_pcsc raw` で `[OS状態] PRESENT`（`MUTE` が付かない）+ `[connect] connect OK ATR=…`
（`check` は reader 名の確認のみで ATR は検査しない）。

## 4. Get UID pseudo-APDU（契約 v1.2 §6 — host が依存する唯一の APDU）

- [ ] host は **`FF CA 00 <k> 00`**（PC/SC Get Data: UID、**P2 = 物理 reader index k**）を送る。
      これに対し **reader k** の UID を **`<UID バイト列> + SW(90 00)`** で返す
      （複数枚は 8B 連結 = §7 / v1.1 §6、UID 昇順）。
      **`k = 0` は v1.0/1.1 の `FF CA 00 00 00` と同一バイト列**（後方互換）。
- [ ] **カード不在・読み取り失敗時**は `90 00` 以外（`6A 81`）を返す。
      host は非 `90 00` を「UID なし（None）」として扱う（`rfid/bridge.py`）。
- [ ] **`k >= 台数` は `6A 86`**（P1/P2 不正）。host の config で `reader` を書き間違えたことが分かる。
- [ ] **`FF CA 00 FF 00` は台数問い合わせ**: `<N>`（1 byte = 物理 reader 数）+ `90 00` を返す。
- [ ] 未通電で skip した index も **範囲内なら `6A 81`**（`6A 86` ではない）。
- [ ] host が要求するのはこの 2 つのみ。ATS/historical（`FF CA 01 00 00`）等は不要（将来 additive）。

**受け入れ**: `probe_pcsc list` に `physical readers: N`。`watch` 実行中にカードをかざすと、
その **reader index** の行に UID が表示される。範囲外 index は `6A 86` で弾かれる。

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

## 8. 複数 reader（本番 11 台。CCID slot は 1 つのまま）

1 台の bring-up が済んだあと、PN5180 を **11 台**（席 8 + board 3: board1 = flop 3 枚重ね /
board2 = turn / board3 = river）に増やすときの追加要件。配線ハーネスと firmware の配線表は
13 台ぶんあるが、本番で有効化するのは先頭 11。**増やすのは物理 reader 台数だけ**で、
USB 上の CCID slot は 1 つのまま（§2 / ADR-0041）＝ **USB 記述子は変わらない**。

- [ ] **1 reader あたりの inventory は 1 回に絞る**（ISSUE-0021）。ドライバの `get_all_uids()` は
      RF 設定 2 種（ASK10/ASK100）× データレート 2 種（high/low）を毎回総当たりし、しかも
      high rate で見つかっても break しないため **1 台で 0.2〜0.9 秒**かかる（実機実測）。
      11 台では 1 周 2〜10 秒になり使えない。firmware は `PN5180_FAST_INVENTORY=1` の自前経路
      （RF ON → 1ms → INVENTORY 1 回 → 応答待ち ≤10ms → RF OFF）を使い、**衝突したときだけ**
      mask を 1 bit 伸ばして分割する。RF 設定は**起動時に 1 回だけ**ロードする
      （`pn5180_init` が共有 RST を pulse するので、**全 reader の init 後**にまとめてロードする）。
- [ ] **見つけた札に STAY QUIET を送り root を再 probe する（capture effect 対策）**。1 slot
      inventory は「mask に合致するのが 1 枚のときだけ応答が成立する」前提だが、実機では
      **2 枚が同時応答しても衝突を検出せず強い方だけを復号する**ことがあり、その枝は 1 枚で
      確定して弱い札が DFS に現れない（実機 2026-09-10: 3 枚重ねで `3 枚` が一度も出なかった）。
      UID を 1 枚見つけるたびに STAY QUIET（`flags=0x22` / `cmd=0x02` / UID 8B **LSB-first**、
      応答なし）で黙らせ、**root(mask 0) を再 probe** して「応答なし」が返るまで繰り返す。
      quiet は **RF off で解除**されるので、**inventory の最後に必ず RF を落とす**こと
      （落とし忘れると次の poll で 0 枚になる。RF 時分割を無効にしていても fast 経路は off する）。
      黙らない札で probe 上限まで空回りしないよう、**新しい UID が増えないラウンドが続いたら
      打ち切る**。
- [ ] **衝突位置（`RX_COLL_POS`）で mask を伸ばし、定常時は確認 probe を間引き、ノイズは分割しない**
      （ISSUE-0021 実機フィードバック 3）。1 bit ずつ伸ばす DFS は、UID の下位ビットが揃った組で
      「札のいない兄弟枝」を RX timeout ぶん舐める（実機 3 枚重ねで probe 14 / 1 周 150 ms）。
      `RX_STATUS` の衝突ビット位置（応答は `flags(8) DSFID(8) UID(64)` なので **UID bit =
      coll_pos - 16**）と衝突前の受信データで prefix ごと伸ばすと 3 枚 = 6 probe。**基準
      （フレーム先頭 / UID 先頭）が実機で違い得るので、受信バイト数と既知ビットを検証して
      合わなければ 1 bit 伸ばしに fallback** する（正しさは Stay Quiet + root 再 probe が担保）。
      さらに、集合が前回と同じ間は「もう居ない」確認 root を数 poll に 1 回に間引く
      （11 台に札が載ると確認だけで ≈90 ms/周）。**衝突フラグが立っていない壊れた受信
      （磁界の縁のノイズ）は COLLISION に倒さない** — 分割しても子枝でノイズが続き probe 上限まで
      空回りする（実機で 1 枚なのに probe 16）。同じ node を 1 回だけ再 probe し、駄目なら「無し」。
- [ ] **presence hold（debounce）は UID 単位**にする。slot 単位（「検出 0 枚のときだけ前回集合を
      保持」）だと、2 枚中 1 枚を 1 回取りこぼしただけで集合が丸ごと置換され、host に届く枚数が
      2↔1 と揺れる（実機 2026-09-10: `probe_pcsc watch` が同じ札を 20 秒で 5 回再発火）。
      UID ごとに連続 miss を数え、**欠けた 1 枚だけ**を数サイクル保持して落とす。
- [ ] **重ね置き（1 reader に複数カード）を全部読む**。席 = hole card 2 枚、board1 = flop 3 枚。
      mask ベースの anti-collision（1 slot inventory + mask 分割 DFS + 上の STAY QUIET）で列挙し、
      **Get UID は UID を uid_len byte ごとに連結**して返す（`count × 8B + 90 00`。1 枚なら
      従来と同一バイト列、0 枚は `6A 81`）。並びは memcmp 昇順に正規化する（host の差分判定用）。
      応答は bulk EP の 64 byte に収めること（8B × 4 枚 + SW = 34 byte まで）。
      契約 **v1.1 §6**（MUST）に準拠。host の分割は `rfid/bridge.py:split_uid_response`
      （応答長 **16/24/32 のときだけ** 8B 分割 = ISO 14443A の 4/7B と衝突させないため）。
- [ ] **RF 時分割: 同時に磁界を張るのは 1 台だけ**。各 reader の inventory 直後に RF を off にする
      （firmware: `PN5180_RF_OFF_BETWEEN_READERS=1` → `pn5180_setRF_off()`）。jef-sure ドライバの
      `get_all_uids()` は **RF を ON のまま戻る**ため、明示的に切らないと 11 台の磁界が同時に立ち、
      隣接アンテナの干渉と電流の積み上がりを招く（ドライバ README も scan 間の off/on を推奨）。
- [ ] **未通電 reader は起動時の MUX scan で skip**する。BUSY が floating の ch は `pn5180_init` を
      **呼ばずに**飛ばし、残りの台で起動する（その index は範囲内なので Get UID は常に `6A 81`）。
      1 台も起動できなければ NSS スキャン診断（BUSY 非依存）を出す。
- [ ] **通電しているのに `pn5180_init` が失敗した reader は、共有 SPI を作り直して 1 回だけ再試行し、
      それでも失敗したらその reader だけ skip して他は続行**する（ISSUE-0023）。ドライバの失敗経路は
      **全 reader 共有の SPI device を外し `pn5180_spi_t` も free する**ため、作り直さずに続行すると
      他の台が壊れる。個別 reader への `pn5180_deinit()` も同じ理由で呼ばない。
- [ ] **init 前に配線表の全 NSS（範囲外の予備も）を output High にする**。init していない chip の NSS が
      floating だと、その chip が MISO を駆動して init 中の reader の応答と衝突し得る（ISSUE-0023）。
- [ ] **`bcdDevice` は `0x0200 | CCID slot 数` = 0x0201 固定**（slot は常に 1）。物理 reader 台数を
      増やしても記述子は変わらないので REV も据え置き。**記述子そのもの（EP 構成 / functional
      descriptor）を変えたときだけ REV を上げる**（Windows は VID/PID/REV で記述子をキャッシュする, §1/契約 §2）。
- [ ] **`bMaxCCIDBusySlots = 1`**（slot 数に連動させない）。実装は bulk OUT を 1 コマンドずつ処理し、
      複数 slot を並行実行しない。
- [ ] **段階 bring-up 1 → 2 → 11**（firmware の `PN5180_READER_COUNT`）。各段階でビルド・書き込みし、
      host 側の受け入れを通してから次へ。SPI クロック（1MHz→5MHz）を上げるのは **11 台が動いてから**、単独で。

**受け入れ**: 各段階で `probe_pcsc list` の **`physical readers: N`** が firmware の台数と一致
（PC/SC の reader 件数は常に 1）。`probe_pcsc watch` で **各 reader index** が
「置く→離す→置く」で再発火し、index↔物理リーダーの対応が config の `pcsc_readers[].reader` どおり。
firmware の UART に出る `poll 統計(直近 N 周): 1 周 min/avg/max = …` で 1 周の実測時間を確認し、
**11 slot で 1 周 ≤ 300 ms**（1 slot なら ≤ 20 ms。**実機 2026-09-10 = 15 ms**）であること。超えるなら
`PN5180_FAST_MAX_PROBES` / `PN5180_FAST_RX_TIMEOUT_MS` / `CARD_POLL_INTERVAL_MS` を見直す
（`PRESENCE_HOLD_MISSES` は**サイクル数**なので、1 周が伸びるとカード離脱の判定時間も同じ比率で伸びる）。
重ね置きは UART の `🎴 reader N: 2 枚 […]` / `3 枚 […]` で枚数を確認し、**置いたまま 20 秒放置して
`probe_pcsc watch` の再発火が 0 件**であること（枚数が揺れていないこと）。

---

## 受け入れマトリクス（契約 § ↔ probe_pcsc ↔ 期待）

| 契約 § | 実装項目 | 確認コマンド | 期待 |
|--------|---------|-------------|------|
| §2 | USB CCID class / VID-PID / product | `probe_pcsc list` | reader_name に product、件数 = slot 数 |
| §3-4 | reader_name（1 件）安定・役割は host config の `reader` index | `probe_pcsc list`（再起動） | matched、名前不変 |
| §3（複数 reader, §8） | 台数 = firmware の `PN5180_READER_COUNT` | `probe_pcsc list` | reader 件数は 1 / `physical readers: N`（1→2→11 の各段階で） |
| §8（poll 周期, ISSUE-0021） | 1 reader 1 回の inventory | firmware UART の `poll 統計` | 1 周 ≤ 300 ms（11 reader）/ ≤ 20 ms（1 reader） |
| §5 | ATR で connect 成立（power-on 常時成功） | `probe_pcsc raw` | `[OS状態] PRESENT`（MUTE 無し）+ `connect OK ATR=…` |
| §6 | Get UID `FF CA 00 00 00`→UID+9000 | `probe_pcsc raw` → `watch` | raw: カード無し `SW=6A81`／置いて `SW=9000`+UID。watch: タップで UID 表示 |
| §7 | UID 4/7/8B 生バイト（MSB-first） | `probe_pcsc watch` | `(8B)`、先頭 `E0:04`（ICODE）、`⚠` 無し、card 解決 |
| §6/§7（重ね置き v1.1, ISSUE-0021） | 複数枚は UID 昇順で連結（`count × 8B + 90 00`） | `probe_pcsc raw`（応答長）→ `watch` | raw: 席 2 枚 = 18 byte / flop 3 枚 = 26 byte。watch: 1 slot から UID が枚数ぶん出る |
| §8 | present/removed・hot-plug（SW で伝達） | `probe_pcsc watch` / 抜き差し | 再タップで再発火、再列挙で復帰 |

全項目 PASS → `python main.py --cli`（`rfid.transport="pcsc"`）で board 3/4/5 枚の street 自動遷移まで
確認（`docs/hardware-qa-checklist.md` 手順5）。これで本番 USB CCID 経路の bring-up 完了。

## 完了後に host 側でやること（firmware 確定値の取り込み）

1. `probe_pcsc list` の実 reader_name を `config.json` の `rfid.pcsc_readers[].name` に等値で記入し、
   `transport` を `"pcsc"` にする。
2. 確定した **VID/PID・実 reader_name** を契約 `docs/contracts/rfid-usb-ccid.md` §2/§4 に追記（ISSUE-0015）。
3. 物理カードを `python tools/register_cards.py run --deck 1`（タップ駆動: 次に置くカードを表示 → 置く →
   登録 → 離す）で `rfid_cards.json`（tag_id→card）に登録。2 デッキ目は `--deck 2`。中断/再開可、
   `list` で不足 code を確認、`unregister <UID>` で修正。

## 関連

- 契約: `docs/contracts/rfid-usb-ccid.md` v1.0（normative） / ADR-0015 / ADR-0034 / ISSUE-0015
- host 実装: `rfid/bridge.py`（Get UID `FF CA 00 00 00` / SW 90 00 / UID 正規化）/ `rfid/reader_thread.py`
  （slot polling / debounce / board_index）/ `rfid/card_master.py`（`normalize_tag_id` / `bytes_to_tag_id`）
- 診断/手順: `tools/probe_pcsc.py` / `docs/hardware-qa-checklist.md`
