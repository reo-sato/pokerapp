# 本番 RFID センサー実機テスト手順 (PC/SC transport)

PC/SC リーダー（例: ACS ACR122U）を PC に直接 USB 接続し、`pyscard` 経由で
NFC タグを読む構成（`transport="pcsc"` / `rfid/reader_thread.py`）の **実機テスト手順**。
ハンドロガー本体までの end-to-end（タグタッチ → UID 読み取り → カード照合 →
`RFIDEvent` → ゲーム状態反映 → ログ出力）を 1 本の流れで検証する。

> **重要 — 実行環境について**
> このテストは **物理 NFC リーダーが USB 接続されたローカル PC** でのみ実行できる。
> CI / クラウド / コンテナ環境にはリーダーが存在しないため、本手順は通らない
> （`RFIDThread` が "No RFID readers connected" で即終了する）。
>
> **HTTP transport（ESP32 + PN532）を使う場合は本手順の対象外。** そちらは
> `rfid/http_receiver.py` / `GET /status` を使う別系統で、本書は扱わない。

関連コード:
- `rfid/bridge.py` — `PCSCBridge`（UID 取得 APDU `FF CA 00 00 00`）, `list_readers()`
- `rfid/reader_thread.py` — `RFIDThread`（ポーリング + デバウンス + `RFIDEvent` 投入）
- `rfid/card_master.py` — `CardMaster`（tag_id → card_code 照合、`rfid_cards.json`）
- `integration/engine.py` — `RFIDEvent` 消費（board → street 推移 / seat → ホールカード蓄積）
- `main.py` `run_cli()` / `run_gui()` — `rfid.enabled` 時に `RFIDThread` を起動

---

## 0. 事前準備チェックリスト

- [ ] PC/SC リーダー（ACR122U 等）を USB 接続済み
- [ ] テスト用 NFC タグ / カード（最低 2〜3 枚）を用意
- [ ] `pyscard` をインストール済み（手順 1）
- [ ] OS の PC/SC ミドルウェアが動作中（手順 2）
- [ ] リーダー名を確認済み（手順 3）
- [ ] `rfid_cards.json` にタグ→カードを登録済み（手順 4）
- [ ] `config.json` を PC/SC 用に編集済み（手順 5）

---

## 1. 依存パッケージのインストール

`pyscard` は `transport="pcsc"` のときのみ必要（`requirements.txt` ではコメントアウト）。

```bash
pip install "pyscard>=2.0.7"
```

確認:

```bash
python -c "import smartcard; print('pyscard OK', smartcard.__file__)"
```

---

## 2. OS 側 PC/SC ミドルウェアの起動

`pyscard` は OS の PC/SC スタックを呼ぶため、それが動いていないとリーダーを認識しない。

### Linux

```bash
sudo apt-get install pcscd libpcsclite1 libccid    # Debian/Ubuntu 系
sudo systemctl enable --now pcscd
pcsc_scan                                           # 接続中リーダーをライブ表示（任意）
```

- **ACR122U で要注意**: カーネルの `pn533` / `pn533_usb` / `nfc` モジュールが
  デバイスを先取りして PC/SC から見えなくなることがある。その場合は
  `/etc/modprobe.d/blacklist-libnfc.conf` に以下を追加して再起動する。

  ```
  blacklist pn533
  blacklist pn533_usb
  blacklist nfc
  ```

### macOS

PC/SC（`pcscd` 相当）は OS 標準で常駐。追加インストール不要。リーダーを挿すだけ。

### Windows

`WinSCard`（Smart Card サービス）が標準で動作。ベンダ提供ドライバ
（ACR122U なら ACS の PC/SC ドライバ）が入っていれば追加作業は不要。

---

## 3. リーダー名の確認（config に書く正確な文字列を得る）

`config.json` の `readers[].name` は **PC/SC が返すリーダー名と完全一致** させる必要がある。
以下で実際の名前を取得する。

```bash
python -c "from rfid.bridge import list_readers; print(list_readers())"
```

出力例:

```
['ACS ACR122U PICC Interface 0']
```

- 空リスト `[]` が返る場合は手順 1〜2 が未完了（pyscard 未導入 / pcscd 停止 /
  リーダー未接続 / カーネルモジュール競合）。先に解消する。
- 複数台つなぐ場合は `Interface 0` / `Interface 1` のように末尾が異なる。
  この文字列を丸ごと（前後空白も含め）控える。

---

## 4. タグ → カードの登録（`rfid_cards.json`）

`rfid_cards.json` は初期状態で **空**（`"cards": {}`）。照合できるカードが 0 件だと
`RFIDEvent.card` が空文字になり、ゲーム状態へは反映されず needs_review 警告だけが出る。
実機テスト前に、使うタグの UID を読み取って card_code を登録しておく。

> **注意**: `rfid_cards.json` の説明文にある `python -m rfid.register` は **未実装**
> （`docs/issues/0007-rfid-register-tool-missing.md` 参照）。当面は以下のいずれかで登録する。

### 4-a. 対話登録スニペット（推奨 / コピペで使う）

リーダー名を手順 3 の値に置き換えて実行する。タグを 1 枚ずつかざし、
プロンプトで card_code（`Ah` `Kd` `Ts` `2c` 等、`Jk`/`JK` はジョーカー）を入力する。
`q` で終了・保存。

```python
# register_cards.py（プロジェクト直下に置いて実行）
import time
from rfid.bridge import PCSCBridge
from rfid.card_master import CardMaster, VALID_CARDS

READER = "ACS ACR122U PICC Interface 0"   # ← 手順3の値に変更

cm = CardMaster("./rfid_cards.json")
bridge = PCSCBridge(READER)
assert bridge.connect(), f"connect failed: {READER}"
print("タグをかざしてください（Ctrl-C で終了）。")

last = None
try:
    while True:
        uid = bridge.read_uid()
        if uid and uid != last:
            last = uid
            existing = cm.lookup(uid)
            print(f"UID={uid}" + (f" (現在 {existing})" if existing else " (未登録)"))
            card = input("  card_code (例 Ah, 空Enterでスキップ): ").strip()
            if card:
                if card not in VALID_CARDS:
                    print(f"  無効: {card!r}")
                else:
                    cm.register(uid, card)   # rfid_cards.json へ即保存
                    print(f"  登録: {uid} -> {card}")
        elif uid is None:
            last = None   # タグを離したら次の同一タグも受け付ける
        time.sleep(0.1)
except KeyboardInterrupt:
    print(f"\n登録済み {len(cm)} 件を ./rfid_cards.json に保存しました。")
```

```bash
python register_cards.py
```

### 4-b. 手動編集

UID が分かっていれば `rfid_cards.json` を直接編集してもよい（UID は
大文字・コロン区切り 16 進に正規化される。`04abcdef1234` でも自動変換される）。

```json
{
  "description": "RFID tag to poker card mapping.",
  "cards": {
    "04:AB:CD:EF:12:34": "Ah",
    "04:AB:CD:EF:12:35": "Kd"
  }
}
```

---

## 5. `config.json` を PC/SC 用に編集

`config_default.json` の `rfid` ブロックは **HTTP 用デフォルト**（`transport: "http"`、
`readers` が dict）。PC/SC では以下の違いに注意して `config.json` を作る。

| 項目 | HTTP（デフォルト） | PC/SC（本手順） |
|------|-------------------|-----------------|
| `transport` | `"http"` | **`"pcsc"`** |
| `readers` | dict（`"seat_1": {...}`） | **list**（`[{"name": ..., ...}]`） |
| 各 reader のキー | `reader_id` 文字列 | **`name`**（手順 3 のリーダー名）必須 |
| ポーリング間隔 | 不使用 | `poll_interval_ms`（既定 100） |

`config.json` が無ければ初回起動時に `config_default.json` がコピーされる。
`rfid` ブロックを次のように差し替える（席数・ロールは卓に合わせる）:

```json
"rfid": {
  "enabled": true,
  "transport": "pcsc",
  "poll_interval_ms": 100,
  "card_master_file": "./rfid_cards.json",
  "readers": [
    {"name": "ACS ACR122U PICC Interface 0", "role": "seat",  "seat": 1},
    {"name": "ACS ACR122U PICC Interface 1", "role": "board"}
  ]
}
```

- `role="seat"` のリーダーには `seat`（席番号）が必須。`role="board"` には不要。
- 1 台だけでテストする場合は `readers` を 1 要素にする。

---

## 6. 単体スモーク（本体を起動せず UID 読み取りだけ確認）

ハンドロガーを起動する前に、リーダー単体でタッチが読めるか最短で確認する。

```bash
python -c "
from rfid.bridge import PCSCBridge
import time
b = PCSCBridge('ACS ACR122U PICC Interface 0')   # ← 手順3の値
print('connect:', b.connect())
print('タグをかざしてください...')
for _ in range(50):
    uid = b.read_uid()
    if uid: print('UID:', uid)
    time.sleep(0.2)
b.close()
"
```

- タッチするたび UID が表示されれば OK。
- `connect: False` → リーダー名不一致（手順 3 をやり直す）。
- ずっと `None` → タグ非対応 / 距離 / モジュール競合（手順 2）。

---

## 7. End-to-end テスト（ハンドロガー起動）

### 7-a. 起動

CLI モードが最短（GUI でも RFID 経路は同じ）。

```bash
python main.py --cli
```

起動時プロンプトで席数・プレイヤー名・スタック・SB/BB・ボタン席を入力する。
RFID が有効なら以下のログが出る:

```
RFID pyscardスレッド起動。
RFIDThread started (N reader(s))
RFID reader ready: ACS ACR122U PICC Interface 0 (reader_0)
```

- 1 台も繋がらないと `No RFID readers connected. RFIDThread exiting.` で
  RFID スレッドだけ終了する（本体は動き続ける）。手順 2〜5 を見直す。

> **ログの読み方**: `RFIDEvent` 内の `reader_id` は config の `name` ではなく
> **接続順インデックス**（`reader_0`, `reader_1`, …）になる。`seat` / `role` は
> config から引き継がれる。

### 7-b. 1 ハンド流して検証

CLI で `n` を入力して新ハンドを開始してから、以下を順にタッチする。

1. **seat リーダーにプレイヤーのホールカードをタッチ**
   - 期待ログ（INFO）: `Hole card detected: seat=1 card=Ah (cards so far: ['Ah'])`
   - 同じ席は最大 2 枚まで蓄積。3 枚目以降は無視される。
   - **未登録タグ**をタッチした場合: `Seat RFID event has no card ... needs_review`（WARNING）。
     → 手順 4 の登録漏れ。

2. **board リーダーにフロップ 3 枚をタッチ**
   - 期待ログ: `Board card [pos=1]: ...` … 3 枚目で
     `Street auto-advanced to flop by RFID board cards (3 cards detected)`
   - 4 枚目 → `turn`、5 枚目 → `river` に自動推移。
   - board リーダーに `board_index` を持たせていない PC/SC 構成では、
     カードは到着順に末尾追記され street 自動推移は行われない（`index` 無しのため）。
     street 推移まで見たい場合は board を複数リーダー＋`index` 付き、または
     HTTP transport を使う（PC/SC の board は枚数蓄積の確認まで）。

3. **デバウンス確認**
   - タグをかざしっぱなしにしても **イベントは 1 回だけ**。
   - 一度離して再タッチすると **再度 1 回** 発火する。

### 7-c. 終了とログ確認

CLI で `q` を入力して終了。最後に保存先が出る:

```
セッション終了。ログ保存先: ./logs/<session_id>.json
```

- 保存された JSON に、タッチで反映された board / ホールカード / street 遷移が
  含まれているか確認する。
- 詳細イベント（`RFIDEvent: reader=... tag=...`）は `RFIDThread` 側では
  **DEBUG レベル**で出る。コンソールに出したい場合は `main.py` の
  `logging.basicConfig(level=logging.INFO)` を一時的に `logging.DEBUG` にする
  （※本番では INFO に戻す）。

---

## 8. トラブルシュート早見表

| 症状 | 主な原因 | 対処 |
|------|----------|------|
| `list_readers()` が `[]` | pyscard 未導入 / pcscd 停止 / 未接続 | 手順 1〜2 |
| `connect: False` | リーダー名不一致 | 手順 3 の文字列を完全一致でコピー |
| `No RFID readers connected.` | `name` 不一致 / 全リーダー connect 失敗 | 手順 3・5 |
| タッチしても `None` のまま | カーネルモジュール競合（Linux/ACR122U） | 手順 2 のブラックリスト |
| `... needs_review`（card 空） | タグ未登録 | 手順 4 で登録 |
| イベントが連発する | デバウンス想定外 / 物理的な抜き差し連打 | 仕様通り。離す→タッチで 1 回 |
| street が進まない | board に `index` 無し（PC/SC） | 7-b の board 注記参照 |
| config を変えても反映されない | `config_default.json` を編集していた | 編集対象は `config.json` |

---

## 9. テスト記録テンプレート（実施時にコピーして埋める）

```
日付:
実施者:
リーダー: （list_readers の出力）
OS / PC/SC: 
登録カード数: （len(CardMaster)）
config readers: 

[ ] 手順6 単体スモーク: UID 読めた？  UID例:
[ ] seat タッチ → Hole card detected ログ出た？
[ ] board タッチ → 枚数蓄積 / street 推移 確認？
[ ] デバウンス（連続→1回 / 再タッチ→再発火）確認？
[ ] 未登録タグ → needs_review 警告 確認？
[ ] 終了後 JSON にカード/street が記録された？

気づき / mismatch:
```
