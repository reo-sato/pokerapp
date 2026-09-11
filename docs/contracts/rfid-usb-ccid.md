# RFID USB CCID firmware ↔ host (PC/SC) contract

**version: 1.3 (ADR-0042。1.0 frozen 起点、1.1 は additive)** ／ canonical RFID transport（ADR-0015）の
firmware↔Python 境界。v1.1 の追加点（1 reader 複数枚の Get UID 連結 / UID MSB-first）、
**v1.2 の変更点（CCID slot は 1 つだけ / 物理リーダーは Get UID の P2 で選ぶ / 台数問い合わせ）**、
**v1.3 の変更点（board reader 全台で 1 つの論理ボードを共有し、位置は検出順で決める =
board の `index` / `cards` を廃止）** は §10 を参照。v1.2 は「slot ごとに reader 名を分ける」規約を
廃止し（Windows の汎用 CCID ドライバが 1 インターフェース 1 slot しか公開しないため。ISSUE-0022 /
ADR-0041）、v1.3 は「board reader = ストリート専用」という前提を廃止する（実機は board reader が
並んでいるだけで、どの台がどのストリートを受けるかは置き方次第。ISSUE-0024 / ADR-0042）。
**firmware 側の要求は v1.0 から一つも変わっていない**（firmware は UID を返すだけで役割・位置を
知らない）。Get UID の `P2=0`・UID 正規化・デバウンスも v1.0/1.1 と同一なので **1 台構成の挙動は不変**。

ESP32-S3（PN5180 ×N）firmware と host Python（pyscard / PC/SC, `rfid/bridge.py` /
`rfid/reader_thread.py`）は別々に実装される。drift を防ぐため、host が依存する **USB descriptor /
reader_name / ATR / pseudo-APDU / UID / hot-plug** の境界を本書で固定する（ISSUE-0015）。

HTTP 経路（`rfid/http_receiver.py`, ADR-0015 で optional secondary）は本契約の対象外（debug/remote 用）。

凡例: **MUST** = 準拠必須 / **SHOULD** = 推奨 / **MAY** = 任意。「host」= Python、「firmware」= ESP32-S3。

---

## 1. 役割と全体像

```
[PN5180 ×N] ──(SPI)── [ESP32-S3 native USB = USB CCID class, slot 1 個] ──(USB)── [host PC]
      ↑ 物理リーダー k は Get UID の P2 で選ぶ                                     OS PC/SC stack
                                                                                  (WinSCard / pcscd)
                                                                                     │ pyscard
                                                                   rfid/bridge.py (PCSCBridge, 1 接続)
                                                                   rfid/reader_thread.py
```

- firmware は PN5180 ×N を **1 つの USB CCID device の 1 slot**（reader_name 1 個）として公開し、
  **物理リーダー k（0..N-1）は Get UID pseudo-APDU の P2 で選ばせる**（v1.2, §3/§6, ADR-0041）。
- host は OS 標準 PC/SC スタック越しに **pyscard** でその reader を列挙し、1 本の接続に
  N 個の Get UID を流して全リーダーを読む。WiFi/HTTP 不要。

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
  記述子をキャッシュする。現在 `0x0102` 系）。
- **slot 数は 1 固定（v1.2 MUST）**: CCID functional descriptor の `bMaxSlotIndex` は **常に 0**
  （= 1 slot）。物理リーダーの台数 N を増やしても **USB 記述子は変わらない**（N は §6 の APDU で
  問い合わせる）ので、台数変更で `bcdDevice` を上げる必要はない。記述子そのものを変える場合のみ
  `bcdDevice` を上げる **MUST**。

## 3. CCID slot は 1 つだけ / 物理リーダーは P2 で選ぶ（firmware MUST / host MUST, v1.2）

- firmware は PN5180 が何台でも **CCID slot を 1 つだけ**公開する **MUST**（`bMaxSlotIndex=0`）。
  **理由（実機で確定, ISSUE-0022 / ADR-0041）**: Windows の Microsoft 汎用 CCID ドライバ（usbccid）は
  **1 インターフェース 1 slot** しかサポートせず、`bMaxSlotIndex=1` にしても PC/SC には
  `PokerRFID PN5180-CCID 0` しか現れない（`… 1` は "Reader not found"）。slot ごとに USB
  インターフェースを分ける回避策は ESP32-S3 の USB endpoint が 6 本しかなく最大 5 台までで、
  本番 11 台に届かない。
- **物理リーダー k（0..N-1）は Get UID pseudo-APDU の P2 で選ぶ** **MUST**（§6）。
  k の割り当ては firmware 内で固定 **MUST**（k=0,1,2,… が常に同じ物理リーダー）。
  - **本番構成（物理 11 台, v1.2）**: `k=0..7` = seat 1..8（各席に hole card **2 枚重ね**）、
    `k=8..10` = **board reader 3 台**（ボード領域に左から右に並べる。**ストリート専用ではない** =
    どの台がどのカードを受けるかは置き方次第, v1.3 §4）。board は「1 リーダー = 1 枚」ではなく
    **1 リーダーに載った枚数ぶん**を読む（§6 の連結応答）。
  - ただし **役割の正準は host config**（§4）であり、firmware は k の順序の安定のみ保証する。
- reader_name（1 個）は **再列挙・再起動を跨いで安定** **MUST**。OS が
  `<manufacturer> <product> <slot index>` 等の形式で描画する（OS 依存, §8）が、
  **安定部分（product 文字列）が変わらない**こと **MUST**。
- v1.1 までの「PN5180 1 個 = CCID 1 slot / slot ごとに一意な reader_name」規約は **廃止**する
  （Linux/pcsc-lite では multi-slot が見えるが、本番 OS は Windows なので分岐を作らない）。

## 4. reader_name ↔ 役割（host config の正準形）

host は canonical PC/SC 経路で `config.rfid.pcsc_readers` を **list** として解釈する **MUST**:

```jsonc
"rfid": {
  "transport": "pcsc",
  "pcsc_readers": [
    {"name": "PokerRFID PN5180-CCID 0", "reader": 0,  "role": "seat",  "seat": 1},
    // … reader 1..7 = seat 2..8（name はすべて同じ = CCID slot は 1 つ, §3）…
    // board は **左から右の順に並べて書く**。位置は config に書かない（検出順で決まる, v1.3）
    {"name": "PokerRFID PN5180-CCID 0", "reader": 8,  "role": "board"},
    {"name": "PokerRFID PN5180-CCID 0", "reader": 9,  "role": "board"},
    {"name": "PokerRFID PN5180-CCID 0", "reader": 10, "role": "board"}
  ]
}
```

- 各要素 = `{"name": <PC/SC reader_name 完全一致文字列>, "reader": 0..254 (任意・既定 0),
  "role": "seat"|"board", "seat": 1..9 (role=seat)}`。**role=board に位置指定は無い**（v1.3）。
- **`reader`（v1.2 追加）** = **物理リーダー index**（= Get UID の P2, §6）。省略時 0 なので、
  1 台構成の v1.0/1.1 の config はそのまま動く。**`(name, reader)` の組が一意**であること **MUST**
  （v1.1 までの「`name` が一意」は廃止 — reader 名は 1 つだけになったため `name` は重複するのが正常）。
  255（`0xFF`）は台数問い合わせ用に予約（§6）。`probe_pcsc check` が範囲・重複を検出する。
- **board は全台で 1 つの論理ボード（v1.3, ADR-0042）**。物理配置は「ボード領域に board reader が
  N 台並んでいるだけ」で、どの台がどのストリートを受けるかは **置き方次第**（flop 3 枚が 3 台に
  散ることも、真ん中の 1 台に 2 枚載ることもある）。よって位置は reader 単位に固定できない。
- **位置割り当て規則（host, v1.3）**: `RFIDEvent.board_index`（1..5）は **board reader 全台を
  通した検出順** = ディーラーが配った順。新規 UID には空き位置の最小を与え、UID が外れたら解放する。
  **一度外して同じ UID を戻すと同じ位置に戻る**（誤って抜いた flop のカードを戻しても board の
  並びが変わらない）。**ボードが 0 枚になったら位置記憶をクリア** **MUST**（= ハンドの切れ目。
  次の flop 1 枚目が前ハンドの位置を継がない）。5 枚（flop 3 + turn + river）を超えたら
  WARN + `board_index=None`（engine は末尾に追記）。
- **board reader は左から右の順に config へ並べて書く** **SHOULD**。同じ poll で台をまたいで
  2 枚以上増えたときの位置順が記載順で決まる（host の poll ループが config 順に回る）。
  **1 台に同時に載った複数枚の左右順は保証しない**（UID 順）= 既知の制約。street 遷移は枚数判定・
  PHH は flop をまとめて出力なので実害は JSON ログの並びのみ（ISSUE-0024）。
- **旧 `index` / `cards`（v1.1/v1.2）は廃止**。残っていても host は**無視**し、起動時に WARN する
  **MUST**（`probe_pcsc check` の lint でも指摘）。「設定したのに効かない」状態を黙って作らない。
- **board reader 1 台だけの構成は警告**する（5 枚を 1 台に重ねることになり給電不足で読めない
  可能性が高い。推奨 3 台以上, ISSUE-0021 OQ6）。
- 席 reader は位置を持たない（hole card は順不同）。2 枚重ねでも `RFIDEvent` が UID ごとに 1 件ずつ
  出るだけで、engine が `seat` ごとに最大 2 枚蓄積する。
- `name` は **OS が描画する reader_name と完全一致** **MUST**（host は前方一致でなく等値で照合,
  `rfid/bridge.py:PCSCBridge.connect`）。OS により文字列が異なるため、運用 OS の実値を入れる（§8）。
- **確定値（Windows, 2026-06-22 実機）**: `PokerRFID PN5180-CCID 0`（manufacturer + product + slot index,
  半角空白区切り）。**slot は常に 1 つなので末尾は常に ` 0`**（v1.2）。Linux/macOS の体裁は別なので、
  運用 OS で `probe_pcsc list` を実行して実 reader_name を確認すること。
- 役割→`RFIDEvent.role`/`.seat` は host config が **唯一の source of truth**。firmware は物理リーダー
  index（P2）の順序のみ保証。`RFIDEvent.reader_id` は従来どおり config の**並び順**（`reader_{i}`）で、
  P2 の値ではない（既存の event 契約は不変, §9）。
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

- **Get UID（v1.2: P2 = 物理リーダー index）**: host は `FF CA 00 <k> 00` を送る（PC/SC v2.01 Part 3
  Get Data: UID の P2 を物理リーダー選択に使う）。firmware は **リーダー k に載っている UID バイト列 +
  SW=`90 00`** を返す **MUST**（`rfid/bridge.py:get_uid_apdu` / `_SW_OK`）。
  - `k=0` は v1.0/1.1 の `FF CA 00 00 00` と**同一**（後方互換 MUST）。
  - カード不在・読み取り失敗時は `90 00` 以外（例 `6A 81` / `63 00`）を返す **SHOULD**（host は非 `90 00` を
    「UID なし」として空扱い）。
  - **k が範囲外（k ≥ N）のときは `6A 86`（Incorrect P1/P2）** を返す **MUST**（v1.2）。host は空扱いに
    加えて **WARN を 1 回だけ**出す（config の `reader` 誤りの検出。`probe_pcsc check` は FAIL 扱い）。
- **物理リーダー台数の問い合わせ（v1.2 additive, MUST）**: host が `FF CA 00 FF 00` を送ったら、
  firmware は **台数 N を 1 バイト + `90 00`** で返す **MUST**（N ≤ 254）。`0xFF` は台数問い合わせ用に
  予約された P2 で、物理リーダー選択には使えない。**v1.1 以前の firmware は非対応**で `6A 81` /
  `6D 00` 等を返すため、host は「非対応 = None」として扱い機能を落とさない
  （`rfid/bridge.py:query_reader_count`、`probe_pcsc list` が表示）。
- **複数 UID の連結（v1.1 additive, MUST）**: 1 台のリーダーに ISO 15693 カードが**複数枚重ねて**置かれた場合
  （席 = hole card 2 枚 / board1 = flop 3 枚）、firmware は載っている全カードの **8 バイト UID を枚数ぶん
  連結**して返す（1 枚 = 8B、2 枚 = 16B、3 枚 = 24B、**最大 4 枚 = 32B**）+ `90 00`。0 枚は `6A 81`（従来どおり）。
  - 並び順は firmware が **UID 昇順**に整列する（安定化のためであって、意味・位置は持たない。
    位置は host が §4 の規則で割り当てる）。
  - **host の分割規則 MUST**: 応答データ長が **16 / 24 / 32 のときだけ 8 バイトずつ分割**し、それ以外
    （4 / 7 / 8 等）は単一 UID として扱う（ISO 14443A の 4/7B と衝突させないため）。
    実装は `rfid/bridge.py:split_uid_response` / `PCSCBridge.read_uids`。
  - anti-collision（複数カードの個別読み出し）は firmware 側の責務（ISSUE-0021）。host は連結された
    応答をパースするだけで、枚数・順序に業務的意味を持たせない。
- v1.2 で host が依存する pseudo-APDU は **Get UID（`FF CA 00 <k> 00`）と台数問い合わせ
  （`FF CA 00 FF 00`）のみ**。ATS/historical bytes（`FF CA 01 00 00`）等は **本契約の対象外**
  （additive に v1.3+ で追加可能）。

## 7. UID 長と正規化（host MUST）

- UID は **4 / 7 / 8 バイト**を取り得る（4=Mifare Classic、7=Type A 7-byte、**8=ISO 15693**）。
  複数枚重ね（§6）のときは 8B UID がその枚数ぶん連結される（16/24/32B）。
- host は UID を **長さ非依存**で扱い、`bytes_to_tag_id` / `normalize_tag_id` で **大文字コロン区切り 16 進**に
  正規化する **MUST**（例: 8B `04 AB CD EF 12 34 56 78` → `04:AB:CD:EF:12:34:56:78`）。`rfid_cards.json` の
  tag_id も同正規化で照合する。
- **バイト順（firmware MUST, v1.1 明文化）**: firmware は UID を **MSB-first**（先頭バイトが UID の最上位
  = ICODE なら `E0:04:…` で始まる）で返す。ISO 15693 の inventory 生レスポンスは **LSB-first** なので、
  firmware 側で反転してから返す（実装済）。host は受け取ったバイト列をそのまま正規化するだけで、
  並べ替えない。カード登録（`tools/register_cards.py`）と `rfid_cards.json` もこの向きで固定される。

## 8. hot-plug / 再列挙 / multi-platform

- **接続は reader 名ごとに 1 本を持続してよい（host, v1.2）**: slot は常時 present（ADR-0040）なので、
  host は poll ごとに `SCardConnect/Disconnect` を繰り返さず、**reader_name につき 1 本の接続を保持**して
  そこに N 個の Get UID を流す（`rfid/bridge.py` の共有接続 + transmit の直列化）。firmware から見ると
  `IccPowerOn/Off` の往復が消え、11 台ぶんの poll が 1 接続の APDU 列になる。transmit が失敗したら
  host は接続を捨てて次の poll で張り直す（USB 抜け・再列挙への耐性は下の項目のまま）。
  firmware は **同一 slot に対する APDU が連続で来ること**を前提にしてよいが、**接続が切れても
  状態を失わない**（次の接続でも同じ k が同じ物理リーダーを指す）**MUST**。
- **card present/removed**: host は各リーダー（P2 = k）を `poll_interval_ms`（既定 100ms）で polling し、
  UID の有無で検出する（`RFIDThread`。同一 UID 連続はデバウンスで 1 回, 外れ→再タッチで再発火）。**カード有無は
  Get UID の SW だけで伝える**（あり: UID + `90 00` / なし: `6A 81` 等）**MUST**。host は CCID の slot 状態
  （bmICCStatus / NotifySlotChange）に依存しないため、firmware は slot を **常時 present** として公開してよい
  （**推奨・Windows では必須**, ADR-0040: 物理有無を slot 状態に反映すると Windows が bind 時に MUTE を
  latch する）。**カード無しで Get UID が `90 00`+UID を返さない**ことが唯一の不変条件 **MUST**。
- **デバウンスは UID 単位（host, v1.1）**: host は reader ごとに「現在載っている UID の**集合**」を保持し、
  poll ごとに **増えた UID だけ** `RFIDEvent` を 1 件ずつ出す（2 枚同時に置けば 2 件）。減った UID は
  イベントを出さず状態のみ更新する（board は §4 の offset を解放）。よって 1 枚だけ外して戻すと
  **その UID だけ**再発火し、置きっぱなしの他の枚は再発火しない（v1.0 の「slot 単位で 1 枚」の
  デバウンスを UID 単位に一般化したもので、1 枚運用時の挙動は同一）。
- **USB 再列挙 / replug**: reader_name の安定部分（§3）が変わらない **MUST**。host の live 再列挙対応
  （実行中の reader 追加・名称変化の追従）は **起動時 connect のみ**（live hot-add は future, ISSUE 追跡）。
  ただし v1.2 の持続接続は transmit 失敗時に自動で張り直すため、**同じ reader_name に戻る replug は
  再起動なしで復帰しうる**（保証はしない）。
- **rfid 切断時**: USB 切断・CCID 再列挙で connect 不能でも host はクラッシュしない（`rfid.enabled=false`
  相当の「RFID なしモード」で継続）**MUST**（エラーハンドリング方針, CLAUDE.md）。
- **multi-platform**: Linux `pcscd` / macOS / Windows WinSCard で動作する **MUST**。reader_name の描画は OS
  依存（OS が product/slot/serial を異なる体裁で連結）。よって `pcsc_readers[].name` は **運用 OS の実 reader_name**
  を入れる（`python tools/probe_pcsc.py list` で確認）。**本番 OS は Windows**（usbccid の single-slot 制限が
  v1.2 の設計制約, §3）。他 OS でも 1 slot + P2 方式はそのまま動くので、OS ごとの分岐は持たない。

## 9. host 不変条件（hardware 非依存・既存契約）

- `RFIDEvent`（`core/events.py`）/ confidence 行列 / reader 役割語彙（`seat_1..9` / `board_1..5`）は
  hardware 非依存で **不変**。本契約が変わっても上流（integration / confidence）は影響を受けない。

## 10. versioning / freeze

- 本契約は **v1.0 frozen**（ADR-0034）。後方互換な追加（新 pseudo-APDU、ATR 種別追加、live hot-add）は
  **minor bump**（1.1, 1.2…）。reader_name 規約・Get UID・UID 正規化の **意味変更は breaking（major）**。
- firmware の確定値（VID/PID、実 reader_name）は確定し次第 §2/§4 に追記する（host コードは変更不要 = 契約安定）。
- **v1.3（2026-09-11, ADR-0042 / ISSUE-0024）** — **board reader 全台で 1 つの論理ボードを共有し、
  位置は検出順で決める**。v1.1 §4 は board reader を「ストリート専用」とし、`index`（先頭ボード位置）
  \+ `cards`（その台に重ねる枚数）を config に書かせていたが、**実機の配置はボード領域に board reader が
  並んでいるだけ**で、どの台がどのカードを受けるかは置き方次第（flop の 2・3 枚目は真ん中の台の方が
  読みやすい）。前提が成立しないため v1.1 の位置モデルを差し替える。**firmware の要求は一つも変わらない**
  （firmware は UID を返すだけで役割・位置を知らない）:
  1. **board に位置指定は無い**（§4）: config は `role: "board"` だけ。旧 `index` / `cards` は
     **廃止**し、残っていても host は無視して WARN する MUST。
  2. **`board_index` は board reader 全台を通した検出順**（§4）= 配った順で 1..5。空き最小を与え、
     外して戻せば同じ位置、**ボードが 0 枚になったら記憶をクリア** MUST（ハンドの切れ目）。
     5 枚超過は WARN + `board_index=None`。
  3. **board reader は左から右の順に config へ並べる** SHOULD（同一 poll で台をまたいだぶんの
     位置順が記載順で決まる）。**1 台に同時に載った複数枚の左右順は保証しない**（既知の制約）。
  4. **lint の変更**（§4）: 「位置の重なり」検査を廃止（概念が消えた）、代わりに **board reader
     1 台だけの構成**を警告（5 枚を 1 台に重ねる = 給電不足の懸念, ISSUE-0021 OQ6）。
  - host 実装: `rfid/reader_thread.py`（`_board_index_by_uid` をスレッドで 1 つ持つ /
    `_warn_obsolete_board_fields`）/ `tools/probe_pcsc.py`（lint 差し替え・config ラベルは位置なし・
    event ラベルは実位置）/ `config_default.json`（board 3 件から `index`/`cards` を削除）。
    回帰テスト: `tests/test_rfid.py::TestBoardGroupPositions`。
- **v1.2（2026-09-10, ADR-0041 / ISSUE-0022）** — **CCID slot は 1 つだけ、物理リーダーは Get UID の
  P2 で選ぶ**。Windows の Microsoft 汎用 CCID ドライバが 1 インターフェース 1 slot しか公開しないことが
  実機で確定したため（`bMaxSlotIndex=1` にしても `… 1` は "Reader not found"）、multi-slot による
  11 台公開を諦め、reader は 1 つのまま P2 で切り替える。**Get UID の意味（P2=0）・UID・デバウンス・
  役割 config の骨格は不変で、1 台構成の挙動は v1.0/1.1 と同一**だが、**「slot ごとに reader 名を分ける」
  規約（v1.1 §3）は廃止**するため minor ではなく「1.1 の一部差し替え」を含む:
  1. **1 slot 固定**（§2/§3）: `bMaxSlotIndex=0`。物理台数が変わっても USB 記述子は不変。
  2. **P2 = 物理リーダー index**（§6）: `FF CA 00 <k> 00`。範囲外は `6A 86`。`k=0` は従来と同一。
  3. **台数問い合わせ**（§6）: `FF CA 00 FF 00` → `<N>` + `90 00`。v1.1 firmware は非対応（host は None）。
  4. **config に `reader`**（§4, 任意・既定 0）: 一意性は `name` から **`(name, reader)`** へ。
  5. **host は reader_name ごとに 1 接続を持続**（§8）: 11 台 = 1 接続 × N APDU。
  - host 実装: `rfid/bridge.py`（`get_uid_apdu` / 共有持続接続 / `query_reader_count` / `6A86` WARN）/
    `rfid/reader_thread.py`（config `reader` → factory）/ `tools/probe_pcsc.py`（lint・`list` の台数表示・
    `check` の SW 判定・`raw --reader`・`watch` の `[rk]`）/ `tools/register_cards.py`（`--reader` で
    config 要素を選択）/ `config_default.json`（11 件・同一 name・`reader` 0..10）。firmware は ADR-0041。
- **v1.1（2026-09-10, additive）** — 1 reader に複数枚を重ねて置く運用（席 = hole card 2 枚 /
  board1 = flop 3 枚）に対応。v1.0 の要求は一つも変更していない（1 枚運用の挙動は同一）:
  1. **Get UID の複数 UID 連結**（§6）: 8B UID × k 枚（k ≤ 4, UID 昇順）+ `90 00`。host は応答長
     16/24/32 のときだけ 8B ずつ分割する。0 枚 = `6A 81` は不変。
  2. **board の `cards`**（§3/§4）: config `pcsc_readers[]` に任意フィールド `cards`（1..5, 既定 1）と
     位置割り当て規則（`index + offset`、検出順・外して戻せば同じ位置）。本番構成は 11 slot
     （席 8 + board 3）。
  3. **UID は MSB-first**（§7）: firmware が ISO 15693 の LSB-first を反転して返す MUST を明文化。
  4. **UID 単位のデバウンス**（§8）: reader ごとの UID 集合差分で発火。
  - host 実装: `rfid/bridge.py`（`split_uid_response` / `read_uids`）/ `rfid/reader_thread.py`
    （集合デバウンス + offset 割り当て）/ `tools/probe_pcsc.py`（lint `cards` / `raw` 分割表示）/
    `tools/register_cards.py`（複数枚検出中は登録しない）。firmware 実装は ISSUE-0021。

## Related

- **`docs/rfid-ccid-firmware-checklist.md`** — 本契約の MUST を ESP32-S3 firmware 実装手順に落とした
  implementer's guide（各項目を `tools/probe_pcsc.py` で受け入れ確認）。
- ADR-0015（PC/SC canonical）/ ADR-0034（本契約 freeze）/ ADR-0040（slot 常時 present）/
  **ADR-0041（1 slot + Get UID の P2 で物理リーダー選択 = v1.2）** /
  ISSUE-0015（本契約の出所）/ ISSUE-0021（複数枚 anti-collision + poll 周期。v1.1 の firmware 側）/
  **ISSUE-0022（Windows usbccid の single-slot 制限 = v1.2 の出所）**
- `rfid/bridge.py`（Get UID / UID 正規化）/ `rfid/reader_thread.py`（pcsc_readers / polling / debounce）
- `rfid/card_master.py`（`normalize_tag_id` / `bytes_to_tag_id`）/ `tests/test_rfid.py`
