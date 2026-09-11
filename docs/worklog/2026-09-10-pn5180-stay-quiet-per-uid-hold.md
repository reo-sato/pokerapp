# Worklog: PN5180 高速 inventory の実機フィードバック対応（Stay Quiet 再 probe + UID 単位 presence hold）

## Date

2026-09-10（同日の `2026-09-10-pn5180-fast-inventory.md` の続き。commit `2ebe914` の実機結果を受けて）

## Scope / Task

commit `2ebe914`（高速 inventory + mask DFS anti-collision）を **実機 1 slot** で動かした結果に
基づく `firmware/esp32s3-pn5180-ccid/main/pn5180_reader.c` の改良（ISSUE-0021 の継続）。
Python / host 側は 1 行も触っていない（firmware + docs のみ）。

## 実機結果（改良前 = commit `2ebe914`, 1 slot）

- `poll 統計`: **カード無しで 1 周 15 ms**（従来 176〜890 ms）。**目標達成**。
- 1 枚: OK。
- **2 枚重ね: 読めるが 2↔1 で数百 ms 周期に揺れる**
  （`2 枚 [E0:04:01:53:1A:41:10:9D, E0:04:01:53:1D:CA:FC:10]`。常に `1D:CA:FC:10` 側が残り
  `1A:41:10:9D` 側が欠ける）。host の `probe_pcsc watch` では欠けた札が 20 秒で **5 回再発火**。
- **3 枚重ね: `3 枚` が一度も出ない**（`1 枚` ↔ `2 枚` のみ。2 枚のときは常に同じ 2 枚）。
- host 側（`raw` / `watch`）の連結分割・UID 単位 event 化は正しく動作。

## Goal

- 3 枚重ねで **`3 枚` が出る**。
- 2 枚 / 3 枚を置いたままで **報告される UID 集合が揺れない**（`watch` が再発火しない）。
- 1 周の所要時間は据え置き（カード無しは 1 probe = 15 ms のまま。probe 上限は有界）。
- 既定 off の切り替えや host 契約（v1.1 §6/§7）は変えない。

## Changed Files

firmware（`firmware/esp32s3-pn5180-ccid/`）:

- `main/pn5180_reader.c` —
  - `ISO15693_FLAG_ADDRESS` / `ISO15693_CMD_STAY_QUIET` を追加。
  - `fast_stay_quiet_15693()` 新規: `flags=0x22`(high rate | Address_flag) + `cmd=0x02` +
    UID 8B（**LSB-first = probe の生バイト順のまま**）の 10 byte を送る。応答が無いコマンドなので
    RX は待たず、`TX_IRQ_STAT` を上限 3 ms の自前ループで待つだけ。失敗は無視。
  - `fast_add_uid()` 新規（重複・満杯を無視して積む小関数）。
  - `fast_inventory_15693()` を **「root probe → 見つけたら Stay Quiet → DFS → root 再 probe」の
    ラウンドループ**に変更。`PROBE_NONE` が返った時点で終了。
    新しい UID が増えないラウンドが 2 回続いたら打ち切る（黙らない札で probe 上限まで
    空回りしないため）。最後に **必ず `pn5180_setRF_off()`**（quiet 解除）。
  - `fast_probe_15693()`: **衝突フラグの判定を受信バイト数 `n == 0` より前に**移動（下記
    Mismatches 3）。
  - presence hold を **UID 単位**に: `s_miss[slot]`（slot 単位）を削除し
    `s_uid_miss[slot][k]`（`s_cache[i].uids[k]` と添字対応）+ `merge_presence()` を追加。
    検出集合と前回集合をマージし、欠けた UID だけを `PRESENCE_HOLD_MISSES`(3) サイクル保持する。
  - `sort_uids()` に `miss` 引数（NULL 可）を追加し、hold カウンタを UID と一緒に並べ替える。
  - 変化時のログに `(hold n)` サフィックス（hold 中の枚数、INFO のまま）。
  - `s_overflow_warned[]`: 「検出枚数が `PN5180_MAX_CARDS_PER_READER` を超えた」ときの WARN を
    slot ごと 1 回に絞る防御分岐（現状は inventory 側が上限で打ち切るため到達しない。将来
    det が上限を超え得る変更に対する保険）。
  - **`diag_after_init_failure()` の NSS スキャンを BUSY 非依存に**（親からの追加依頼、下記
    「追加: init 失敗時の診断」）: BUSY が High（floating/stuck）でも SPI を送り、
    READ_EEPROM(0x07, addr 0x12 = FIRMWARE_VERSION, len 2) の応答で「チップ生存」を判定する。
- `main/app_config.h` —
  - `PN5180_FAST_MAX_PROBES` **12 → 16**（root 再 probe と最後の「応答なし」確認ぶん）。
    見積りコメントを Stay Quiet 込みの数（無 1 / 1 枚 2 / 2 枚 3〜5 / 3 枚 4〜8）に更新。
  - `PN5180_FAST_INVENTORY` のコメントに Stay Quiet 再 probe と「最後に必ず RF off」を追記。

docs:

- `docs/issues/0021-pn5180-poll-cycle-latency.md` — 「実機結果（2026-09-10, commit `2ebe914`）」節
  （結果 / 追加 Root Cause 3 点 / 対処 / 改訂 probe 表）、Regression Check に「3 枚重ねで `3 枚`」と
  「20 秒放置で再発火 0 件」、Open Question 5（給電不足の切り分け）、Notes の書き換え。
- `firmware/esp32s3-pn5180-ccid/README.md` — 「高速 inventory + 重ね置き anti-collision」項目に
  Stay Quiet 再 probe / UID 単位 hold / 実機 15 ms を追記。
- `docs/rfid-ccid-firmware-checklist.md` §8 — Stay Quiet 再 probe と UID 単位 hold を MUST 項目に追加。
- `CHANGELOG.md` `[Unreleased]` に Fixed ブロック。
- `CLAUDE.md` 実装状況表の firmware 行を実機結果込みに更新。
- 本 worklog。

## Expected Behavior

- **Stay Quiet + root 再 probe**: 1 枚見つけるたびにその札を黙らせ、root(mask 0) をやり直す。
  capture effect（2 枚同時応答を PN5180 が衝突と見なさず強い方だけ復号する）で隠れていた札が、
  強い札が黙ったあとに応答してくる。「応答なし」が返った時点で全枚数を拾い終わり。
- quiet は磁界が消えるまで続くので、**fast 経路は最後に必ず RF off**（`PN5180_RF_OFF_BETWEEN_READERS=0`
  でも）。これを忘れると次の poll で 0 枚になる。
- **UID 単位 hold**: 2 枚中 1 枚を取りこぼしても、その UID を 3 サイクル保持するので host に
  伝わる集合は変わらない（= `watch` が再発火しない）。3 サイクル続けて欠けたらその 1 枚だけ落ちる。
- カード無しの reader は従来どおり **1 probe**（1 周 15 ms は維持）。

## Implemented Behavior

上記のとおり。**ESP-IDF がこの container に無いため実ビルド・実機検証は未実施**。代わりに
スタブ・フルコンパイルと register-level simulator で検証した（下記 Test Results）。

実装上の判断:

- Stay Quiet は **PROBE_UID なら重複でも送る**（前回の送信が届かなかった場合の再送になる）。
- それでも黙らない札（規格外・TX 失敗が続く）に備え、**新しい UID が増えないラウンドが 2 回
  続いたら打ち切る**。1 回だけ再試行を許すのは「TX を 1 回取りこぼしただけ」の救済のため。
- `merge_presence()` は UID 長が変わった（15693 8B ↔ 14443 4/7B）ときだけ前回集合を捨てる
  （cache が uid_len を 1 つしか持てないため）。
- 満杯時に新しい UID が来たら **hold 中（miss>0）で最も古いものを追い出す**。「今そこにある札」を
  hold 中の残像より優先する。

## 追加: init 失敗時の診断（`diag_after_init_failure`）を BUSY 非依存に

実機で「MUX scan 全 ch floating / RST 診断 BUSY=1,1,1 / NSS スキャンは全候補『送信前から
BUSY=High（判定不能）』で **SPI を一度も送らずに終了**」という状態が出て、
**PN5180 が死んでいるのか BUSY(MUX)経路だけが壊れているのか切り分けられなかった**。

- BUSY が High でも **必ず SPI を送る**（BUSY ハンドシェイクの代わりに固定待ち 1 ms）。
  送信前 BUSY が High だった候補は「BUSY Low→High」判定だけを諦める（誤検出して再 init すると
  クラッシュするため。既存の反省を維持）。
- 送るのは **READ_EEPROM**（cmd `0x07` / addr `0x12` = FIRMWARE_VERSION / len 2）。PN5180 の SPI は
  「送信 = NSS↓ コマンド NSS↑」「受信 = NSS↓ 読み出し NSS↑」の 2 フェーズなので、hardware CS の
  `spi_device_polling_transmit` を **2 回**に分ける（各フェーズ後に 1 ms 待つ）。
- 受信 2 byte を `FW=%02X %02X` で表示。`FF FF` / `00 00` 以外なら
  **「SPI 応答あり = PN5180 は生きている → BUSY/MUX 経路（MUX VCC/EN/SIG・その ch の BUSY 線）を
  疑う」**、全候補が `FF FF`/`00 00` なら **「SPI 無応答 = 電源/RST/SPI 配線」** と結論を出す。
  切り分け手順（`PN5180_BUSY_VIA_MUX=0` + BUSY 直結）もログに出す。
- 候補は `PN5180_READERS` の **13 本すべて**（ハードコード配列を廃止して配線表を単一の source に）。
  ログに MUX ch も出す。BUSY が Low→High に動いた候補は従来どおり最優先（見つけたら break）。

## Test Results

ESP-IDF が無いので実ビルドの代わりに以下（`gcc 13` + ESP-IDF/ドライバのスタブヘッダ）。

### 1. スタブ・フルコンパイル

- `CCID_SLOT_COUNT`(1/2/11/13) × `POLL_STATS_INTERVAL_MS`(0/10000) ×
  `PN5180_RF_OFF_BETWEEN_READERS`(0/1) × `PN5180_BUSY_VIA_MUX`(0/1) × `PN5180_TRY_ISO14443`(0/1) ×
  `PN5180_FAST_INVENTORY`(0/1) = **128 構成で警告 0 / エラー 0**（`-c -std=gnu17 -Wall -Wextra`）。
- 既定構成に対して `-Wshadow -Wundef -Wvla -Wformat=2 -Wpointer-arith -Wcast-align
  -Wstrict-prototypes -Wmissing-prototypes -Wswitch-enum -O2` でも **警告 0**。
- `CCID_SLOT_COUNT` = 0 / 14 では `_Static_assert` が意図どおりビルドを止める。

### 2. register-level simulator（実 `pn5180_reader.c` / `ccid_slot.c` をリンク、ASan/UBSan）

前回の simulator に **capture effect**（複数枚が同時応答したとき衝突フラグを立てず「強い方」だけを
返す）、**STAY QUIET**（宛先タグが黙る / RF off で解除 / フレーム長・flags を検証）、
**取りこぼし**（指定タグが 1 poll だけ応答しない）、**quiet 失敗**（特定タグに届かない）を追加。

| シナリオ | 結果 | probe 数 |
|---|---|---|
| capture, 2 枚重ね（3 poll 連続） | 毎回 **2 枚**（順序も安定） | 3 |
| capture, 3 枚重ね（3 poll 連続） | 毎回 **3 枚**（RF off で quiet が解除され毎 poll 全数を再取得） | 4 |
| capture, 3 枚 + tag0 への quiet が届かない | 3 枚取得、**probe 上限内で終了** | 5 |
| 2 枚のうち 1 枚が 1 poll 欠ける | 報告 set が**不変**（hold） | — |
| 同上 2 poll 連続で欠ける | 報告 set が**不変** | — |
| 同上 3 poll 連続で欠ける | **その 1 枚だけ**離脱、残り 1 枚は保持 | — |
| 欠けた札が戻る | miss リセットで 2 枚に復帰 | — |
| 全部外す | 3 サイクルで present=0 | — |
| （旧シナリオ）0/1/2/3/5 枚・ノイズ・debounce | 前回同様に正しい cache と Get UID バイト列 | 1 / 2 / 3〜 |
| Stay Quiet を一切実装しない simulator | 全数取得できたうえで**打ち切りが効く**（1 枚 3 probe / 2 枚 9 probe） | ≤ 16 |
| `PN5180_FAST_INVENTORY=0`（ドライバ経路） | 同じ cache 形式・UID 単位 hold も同様に動作 | — |

### 3. init 失敗時の診断（別 simulator）

`pn5180_init` を NULL 返しにし、BUSY を常時 High に固定して `diag_after_init_failure` を走らせた。

| ケース | 結果 |
|---|---|
| 全 NSS で MISO=`FF FF` | 13 候補すべてに SPI を送信（送信 13 / 受信 13 フェーズ）→ `❌ 全候補で SPI 無応答 … 電源/RST/SPI 配線` |
| NSS=GPIO4 だけ `FW=0C 03` | 同上 13 候補に送信 → `✅ SPI 応答あり (NSS=GPIO4, FW=0C 03) = PN5180 は生きている → BUSY/MUX 経路を疑う` + 切り分け手順 |

送信フェーズが `07 12 02`（3 byte）、受信フェーズが 2 byte であることも simulator 側で検証。

### 4. Python

- `pytest tests/ -q --ignore=tests/test_vision.py` → **774 passed**（36s、変更前と同数）。
  本タスクは Python を 1 行も触っていない。

## Mismatches Found During Testing

1. **`PROBE_UID` の重複時に Stay Quiet を送らないと打ち切れない**: 最初の実装は「重複 UID なら
   何もしない」だったが、それだと quiet が届かなかった札で root probe が同じ UID を返し続け、
   probe 上限まで空回りする（simulator で 1 枚に 16 probe を消費）。
2. **quiet が効かない札での時間爆発**: 上を直しても「衝突 → DFS で 2 枚発見 → quiet 効かず →
   root がまた衝突」のループが残り、2 枚で毎 poll 16 probe（≈ 80 ms/台）になった。
3. **（重大・既存バグ）衝突が「カード無し」に倒れていた**: simulator に commit `2ebe914` の
   コードをリンクして走らせると、**2 枚 / 3 枚 / 5 枚のどれでも probe 1 回で終わり、分割が一度も
   起きなかった**。原因は `fast_probe_15693` が `RX_STATUS` の受信バイト数を先に見て
   `n == 0 → PROBE_NONE` としており、**「collision=1 / 受信 0 byte」で返る衝突**（SOF で衝突した
   ケース）を「カード無し」と判定していたこと。実機で 2 枚が読めていたのは、衝突が
   「バイトはあるが CRC/protocol error」の形で返る回があったため（そちらは分割側に倒していた）。
   前回 worklog の simulator 表（2 枚 = 3 probe 等）は、この分岐に到達しない条件で取った数字
   だったことになる。

## Fixes Applied

1. `PROBE_UID` なら**重複でも Stay Quiet を送る**（黙らせ損ねの再送）。
2. **ラウンド単位の進捗チェック**: 新しい UID が 1 枚も増えないラウンドが 2 回続いたら打ち切る
   （`stale_rounds`）。simulator の「quiet 未実装」ケースで 1 枚 3 probe / 2 枚 9 probe に収束。
3. `fast_probe_15693` で **衝突フラグを受信バイト数より先に判定**する（`collision` なら n に依らず
   `PROBE_COLLISION`）。これで simulator の 2/3/5 枚が正しく分割されるようになった。
4. `PN5180_FAST_MAX_PROBES` を 16 に上げ、DFS スタック上限（`PN5180_FAST_MAX_PROBES + 2`）と
   コメントの見積りを合わせた。

## Remaining Gaps / Out-of-Scope

- [ ] **3 枚重ねの実機確認**（本タスクの主目的）: UART に `🎴 reader N: 3 枚 [...]` が出るか。
- [ ] **ちらつきの実機確認**: 2 枚 / 3 枚を置いたまま 20 秒で `probe_pcsc watch` の再発火 0 件か。
- [ ] **給電不足だった場合は firmware では解決できない**: 密着した 2 枚が磁束を食って 3 枚目が
      動作電圧に届かないケース。切り分けは「3 枚のうち任意の 2 枚だけなら必ず 2 枚読める」か
      （読めるなら分離の問題 = 本タスクの対象、どの組でも 3 枚目だけ出ないなら電力不足）。
      対処は `PN5180_FAST_FIELD_SETTLE_US` を伸ばす / `PN5180_FAST_RF_CONFIG` を ASK10 に振る /
      カードを少しずらす / アンテナ出力を上げる（ISSUE-0021 Open Question 5）。
- [ ] **1 周時間の再実測**: 確認 probe のぶん、カードが載っている reader は +10 ms 程度増える
      （空の reader は 1 probe のまま）。11 slot 全部にカードを置いて 300 ms を超えるなら
      `PN5180_FAST_RX_TIMEOUT_MS`（10 ms。実応答は ≈ 4 ms）を先に詰める。
- [ ] **11 台への段階 bring-up**（1 → 2 → 11）と `PN5180_SPI_HZ` 1MHz → 5MHz は未消化のまま。
- [ ] `PN5180_FAST_RF_CONFIG` の ASK100 / ASK10 を実機で確定（前 worklog から継続）。
- [ ] `RX_COLL_POS` を使った probe 数削減は未実装（ISSUE-0021 Open Question 4）。
- [ ] `merge_presence()` の「上限超過 WARN」は現状到達しない防御分岐（inventory 側が上限で
      打ち切るため）。将来 det が上限を超え得る形に変えたときのための保険。
- [ ] **診断の READ_EEPROM 応答は実機未確認**: PN5180 の firmware version は 2 byte（例 `0C 03`
      = 3.12）で返る想定だが、実チップでの値・SPI 2 フェーズ間の待ち（1 ms）が足りるかは
      実機で確認する。`FW=` が全候補 `FF FF` でも、待ちが短すぎるだけの可能性は残る
      （その場合は固定待ちを 2〜5 ms に伸ばす）。

## Related ADRs

- `docs/adr/0034-rfid-usb-ccid-firmware-host-contract-freeze.md` — 契約 v1.0 / v1.1 §6 の連結応答。
  本タスクは firmware 内部の読み取り手順のみで、**契約は変更なし**。
- `docs/adr/0040-ccid-virtual-card-always-present.md` — 0 枚 = `6A 81` はこの設計のまま。

## Related Issues

- `docs/issues/0021-pn5180-poll-cycle-latency.md` — 本タスクの issue（Fixed / 実機確認中）。

## Related Commits

- `2ebe914` — 高速 inventory + mask DFS anti-collision（本タスクの改良対象）。
- （本 worklog と同一の変更セット）Stay Quiet 再 probe / UID 単位 presence hold / 衝突判定順の修正。
  **未コミット**（親のレビュー後にコミットする運用）。
