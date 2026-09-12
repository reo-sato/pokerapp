# 実機 RFID QA チェックリスト（PC/SC canonical, Phase H）

実機の **ESP32-S3 + PN5180（USB CCID → PC/SC, canonical, ADR-0015/0034/0041）** を接続して、
RFID 経路を `docs/contracts/rfid-usb-ccid.md` **v1.2** の MUST に対して bring-up 確認する手順。

- **v1.2（ADR-0041）**: PC/SC の reader（CCID slot）は **1 つだけ**で、物理リーダー 11 台
  （席 8 + board 3）は **Get UID の P2**（config の `reader` 0..10）で選ぶ。よって手順 1 で見える
  reader_name は **1 個が正常**。
- **v1.1**: 1 台のリーダーに複数枚重ね置き（席 2 枚 / flop 3 枚）。

- 実機なしの確認は [`manual-qa-checklist.md`](manual-qa-checklist.md)（音声のみ必須経路 + 項目6 の
  HTTP 模擬）を参照。本書は **実機が要る項目**（🖥️）に特化する。
- 診断には [`tools/probe_pcsc.py`](../tools/probe_pcsc.py) を使う（production と同じ
  `rfid.bridge.PCSCBridge` / `rfid.reader_thread.RFIDThread` を叩くため、ここで OK なら hand logger でも OK）。
- 契約の正準は `docs/contracts/rfid-usb-ccid.md`。本書は手順、契約は規約。食い違いは契約が優先。
- **firmware を書く人向け**の MUST 実装チェックリストは [`rfid-ccid-firmware-checklist.md`](rfid-ccid-firmware-checklist.md)
  （本書のホスト側検査と対。sensor が USB CCID として PC に出る firmware が前提）。

---

## 0. 前提セットアップ 🖥️

```bash
pip install ".[pcsc]"          # pyscard（PC/SC canonical 経路に必須）
# Linux: pcscd を起動（例: sudo systemctl start pcscd / sudo pcscd -f）
# macOS: 標準で PC/SC 稼働。Windows: WinSCard（標準）
```

- **期待**: `python -c "import smartcard; print('ok')"` が `ok`。
- **見る点**: ESP32-S3 を USB 接続し、OS が **USB CCID（Smart Card）class** として認識していること
  （HID/シリアルではない, 契約 §2）。Linux は `lsusb` に CCID デバイス、`pcsc_scan` でも可。

## 1. reader 列挙と reader_name 確定 + 物理リーダー台数（契約 §3-4 / §6 / §8）🖥️

```bash
python tools/probe_pcsc.py list
```

- **期待**: 接続中の PC/SC reader_name が **1 個だけ**並ぶ（v1.2: CCID slot は 1 つ, 契約 §3 / ADR-0041。
  物理リーダーが 11 台でも reader 名は 1 個）。その行に `physical readers: N`（firmware が
  `FF CA 00 FF 00` に返した台数）が出る。下段に config との突き合わせ
  （`matched` / `MISSING` / `unconfigured`）が `(name, reader)` 単位で並ぶ。
- **見る点**:
  - reader_name は OS 依存の文字列。**ここに出た文字列を 1 文字違わず** `config.rfid.pcsc_readers[].name`
    に等値で入れる（前方一致しない, 契約 §4）。**11 件すべて同じ文字列**になる。
  - `physical readers: N` が実際に繋いだ台数と一致すること。`(v1.1 firmware: 台数問い合わせ非対応)` と
    出る場合は firmware が v1.2 未対応（動作はするが台数チェックができない）。
  - `⚠ reader k は firmware の台数 N を超えている` が出たら config の `reader` を直す（§6, `6A 86` 相当）。
  - 再起動 / USB 再挿入を跨いで **安定部分（product 文字列）が変わらない**こと（§3/§8）。
  - product 文字列の推奨は `PN5180-CCID`（§2）。**確定した VID/PID・実 reader_name は契約 §2/§4 に追記**
    する（ISSUE-0015 の実環境残作業）。

## 2. config 設定と connect 検査（契約 §4-5）🖥️

`config.json`（無ければ `config_default.json` をコピー）の `rfid` を canonical に設定する:

```jsonc
"rfid": {
  "enabled": true,
  "transport": "pcsc",
  "poll_interval_ms": 100,
  "card_master_file": "./rfid_cards.json",
  "pcsc_readers": [
    {"name": "<手順1 の実 reader_name>", "reader": 0,  "role": "seat",  "seat": 1},
    // … reader 1..6 = seat 2..7（name は全要素で同じ文字列）…
    {"name": "<手順1 の実 reader_name>", "reader": 7,  "role": "seat",  "seat": 8},
    {"name": "<手順1 の実 reader_name>", "reader": 8,  "role": "board", "index": 1, "cards": 3},  // flop 3 枚重ね
    {"name": "<手順1 の実 reader_name>", "reader": 9,  "role": "board", "index": 4},              // turn
    {"name": "<手順1 の実 reader_name>", "reader": 10, "role": "board", "index": 5}               // river
  ]
}
```

本番は **物理 11 台**（席 8 + board 3）で、**reader 名は 1 つ**・`reader`（Get UID の P2）で台を選ぶ
（契約 v1.2 §4 / ADR-0041）。board は 1 台 = 1 枚ではなく、flop の 3 枚を 1 台に重ねて置く
（`cards`, 契約 v1.1 §4）。席リーダーは 2 枚重ねでも `cards` を書かない（hole card は位置を持たない）。

```bash
python tools/probe_pcsc.py check
```

- **期待**: `config lint` が `✓`、各 reader が `✓ PASS  seat N [rK] … Get UID OK（カード無し）`、
  最後に `PASS ✅`。
- **見る点**:
  - lint は name 欠落・role 不正・seat 1..9 外・seat 重複・board index 1..5 外・**`reader` 0..254 外**・
    **`(name, reader)` の重複**に加え、**`cards` 1..5 外 / `cards>1` なのに index 無し /
    `index+cards-1` が 5 超 / board 位置の重なり**を検出する（§4, v1.1/v1.2）。
    **`name` の重複は v1.2 では正常**（reader 名は 1 つなので全要素で同じ）。
  - PASS = OS PC/SC が **ATR を受理**して `SCardConnect` 成功（host は ATR 非依存, §5）+
    `FF CA 00 <k> 00` に `90 00`（カードあり）か `6A 81`（カード無し）が返る。
  - `✗ FAIL … reader K は firmware の範囲外 (SW=6A86)` → config の `reader` が firmware の台数を
    超えている（手順1 の `physical readers: N` と突き合わせる, §6）。
  - connect FAIL は name 不一致が最多 → 手順1の文字列を再確認。
  - **注**: `poll_interval_ms` はキーに先頭 `_` を付けない（`_pcsc_poll_interval_ms` はコメント扱いで無効）。

## 3. カード UID の登録（`rfid_cards.json`）🖥️

物理カードの UID を読んで `tag_id → card_code` を登録する。**タップ駆動の登録ツール**を使う
（「次に置くカード」が表示され、置くと登録、離すと次へ。1 枚ごとに保存されるので途中で止めてよい）:

```bash
python tools/register_cards.py run --deck 1          # 1 デッキ目: ♠A..K ♥ ♦ ♣ + ジョーカー 2 枚の順
python tools/register_cards.py run --deck 2          # 2 デッキ目（同じ code に別 UID を追加）
python tools/register_cards.py list --deck 2         # 不足 code の確認（再開はもう一度 run）
python tools/register_cards.py unregister <UID>      # 置き間違えの修正
# 登録に使う物理リーダーは --reader で選ぶ（config の index か 'seat 1' / 'board 1'。既定は先頭要素）
python tools/register_cards.py run --deck 1 --reader "seat 1"
# 新品デッキの並びが違うときは --order rank-suit / --only Ah,Kd / --start-at Kd で順序を合わせる
```

- **期待**: `✓ [ 1/54] As ← E0:04:…` のように 1 枚ずつ登録され、最後に `deck 1: 54/54 済 ✅ 完了`。
- **見る点**: ISO 15693 は **8 バイト**、先頭 `E0:04`（ICODE）。別 code で登録済みの UID を置くと `⚠` で
  拒否される（置き間違い防止）。手で書く場合も tag_id は大文字コロン区切り（`normalize_tag_id`）。
  未登録の UID は手順4の `watch` に `card=(未登録)` として出る。

## 4. ライブ・タップ確認（UID / 役割 / hot-plug, 契約 §6-8）🖥️

```bash
python tools/probe_pcsc.py watch --seconds 30
```

各リーダーで「**置く → 離す → もう一度置く**」を行う。**重ね置き**（席に 2 枚 / board1 に 3 枚）も試す。

- **期待**: タップごとに `seat N [rK]` / `board M [rK]` のラベル + 正規化 UID + バイト長 + 解決カードが
  1 行出る（`[rK]` = config の `reader` = 物理リーダー index, v1.2 §6）。
  - **席に hole card 2 枚を重ねて置く → 2 行**（同じ `seat N [rK]`、UID が別）。
  - **board1 に flop 3 枚を重ねて置く → `board 1` / `board 2` / `board 3` の 3 行**（位置は検出順に
    割り当て。1 枚だけ外して戻すと**同じ位置**でもう一度出る。契約 v1.1 §4/§6/§8）。
  - `cards` を超える枚数を載せると WARN が出て `board` ラベル（位置なし）になる。
- **見る点**:
  - **役割マッピング**（§4）: 物理リーダーの置き場所と `seat`/`board` ラベル（と `[rK]`）が一致するか。
    ズレていれば `pcsc_readers` の `reader`（P2）↔役割の対応を直す（firmware は P2 の順序のみ保証、
    役割は host config が source of truth）。**11 台では 1 台ずつ順にタップして `[rK]` を照合する**のが
    最短（k を 1 つ間違えると席がまるごと入れ替わる）。
  - **Get UID**（§6）: タップで毎回 UID が取れる（`FF CA 00 <k> 00` → UID + `SW=90 00`）。
    特定の台だけ 0 件なら `python tools/probe_pcsc.py raw --reader <k>` でその台を直接叩いて切り分ける。
  - **デバウンス / hot-plug**（§8）: 置きっぱなしは 1 回だけ発火、離して再度置くと再発火する。
  - 登録済みカードは card 列に `Ah` 等、未登録は `(未登録)`（hand logger では `needs_review` 経路）。

## 5. hand logger 通し（board street 自動遷移 + corroboration）🖥️

実際の hand logger に RFID を流し込み、ストリート遷移と confidence を確認する。

```bash
python main.py --cli           # 起動ログに "RFID pyscardスレッド起動。"
```

席カードとボードを順にタップ（フロップ3 → ターン4 → リバー5）する。

- **期待**: board 枚数 3/4/5 で flop→turn→river が自動遷移し、ハンド確定で `logs/<session>.json` に追記。
- **見る点**: `RFIDEvent` のログ（role/seat/board_index/card）。音声と近接した RFID は confidence を上げる
  （[`manual-qa-checklist.md`](manual-qa-checklist.md) 項目6 の corroboration と同じ。音声併用時）。
  board の `index` が無い設定だと street が進まないので手順2で `index` を入れること（§4, B3 回帰）。

## 6. 切断レジリエンス（契約 §8 / エラーハンドリング方針）🖥️

- 稼働中に ESP32-S3 の USB を抜く → host が **クラッシュしない**こと（`rfid.enabled=false` 相当の
  「RFID なしモード」で hand logger は継続, 契約 §8 / CLAUDE.md エラーハンドリング方針）。
- 再挿入後の reader_name の安定部分が不変であること（§3/§8）。**注**: 稼働中の live 再列挙
  （hot-add）は v1.0 では起動時 connect のみ。再挿入後は再起動で拾い直す（future 項目）。

## 7. マルチプラットフォーム（契約 §8, 任意）🖥️

- Linux `pcscd` / macOS / Windows WinSCard で手順1-5 が動くこと。reader_name の体裁は OS ごとに
  異なるため、**運用 OS ごとに手順1で実 reader_name を取り直して** config を分ける。

---

## 受け入れ基準（契約 v1.2 ↔ 本手順）

| 契約 § | 項目 | 確認手段 |
|--------|------|---------|
| §2 | USB CCID class / VID-PID / product 文字列固定 / slot 数 1 固定 | 手順0（OS 認識）+ 手順1（reader_name に product・1 個） |
| §3 | CCID slot は 1 つ / reader_name 安定 / 物理 11 台は P2 で選ぶ | 手順1（reader 1 個 + `physical readers: N`・再起動跨ぎ） |
| §4 | `(name, reader)`↔役割・`cards` と board 位置（host config が正準・等値照合） | 手順2 `check`（lint）+ 手順4（`[rK]` と役割/位置一致） |
| §5 | PC/SC 互換 ATR で connect 成立 | 手順2 `check`（PASS） |
| §6 | Get UID `FF CA 00 <k> 00` / 範囲外 `6A 86` / 台数 `FF CA 00 FF 00` / 複数枚は 8B × k 連結 | 手順1（台数）+ 手順2（SW 判定）+ 手順4 `watch` + `raw --reader k`（`UID×k = …`） |
| §7 | UID 4/7/8B 正規化 / MSB-first（`E0:04:…`） | 手順3/4（`(8B)` 表示・`⚠` 無し・先頭 `E0:04`） |
| §8 | 1 接続持続 / UID 単位デバウンス / hot-plug / 切断耐性 / multi-platform | 手順4（1 枚だけ外して戻すとその UID だけ再発火）+ 手順6（USB 抜き）+ 手順7 |

すべて PASS かつ board street 自動遷移（手順5）まで確認できれば、RFID 実機経路の bring-up 完了。

## 実環境で確定して契約へ追記すべき項目（ISSUE-0015 / ISSUE-0022 残）

- firmware の **VID/PID** と **実 reader_name** は §2/§4 に追記済（2026-06-22 実機, Windows）。
- **物理リーダーの段階検証（ISSUE-0022 / ADR-0041）**: 1 台 → **2 台**（`reader` 0/1 で `[r0]`/`[r1]` が
  出るか）→ **11 台**（席 8 + board 3）の順に手順1-5 を回す。11 台では poll 1 周（1 接続 × 11 APDU）の
  所要時間を計測し、`poll_interval_ms` を決める（ISSUE-0021）。
- live hot-add（稼働中の reader 追加追従）は future（起動時 connect のみ。v1.2 の持続接続は
  transmit 失敗時に張り直すので、同名 reader への replug は復帰しうる）。
