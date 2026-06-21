# 実機 RFID QA チェックリスト（PC/SC canonical, Phase H）

実機の **ESP32-S3 + PN5180（USB CCID → PC/SC, canonical, ADR-0015/0034）** を接続して、
RFID 経路を `docs/contracts/rfid-usb-ccid.md` **v1.0** の MUST に対して bring-up 確認する手順。

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

## 1. reader 列挙と reader_name 確定（契約 §3-4 / §8）🖥️

```bash
python tools/probe_pcsc.py list
```

- **期待**: 接続中の PC/SC reader_name が slot 数だけ並ぶ（PN5180 1 個 = 1 slot, 契約 §3）。
  下段に config との突き合わせ（`matched` / `MISSING` / `unconfigured`）が出る。
- **見る点**:
  - reader_name は OS 依存の文字列。**ここに出た文字列を 1 文字違わず** `config.rfid.pcsc_readers[].name`
    に等値で入れる（前方一致しない, 契約 §4）。
  - 再起動 / USB 再挿入を跨いで **安定部分（product 文字列 + slot index）が変わらない**こと（§3/§8）。
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
    {"name": "<手順1の実 reader_name>", "role": "seat",  "seat": 1},
    {"name": "<別 slot の実 reader_name>", "role": "board", "index": 1}
  ]
}
```

```bash
python tools/probe_pcsc.py check
```

- **期待**: `config lint` が `✓`、各 reader が `✓ PASS`、最後に `PASS ✅`。
- **見る点**:
  - lint は name 欠落 / 重複・role 不正・seat 1..9 外・seat 重複・board index 1..5 外を検出する（§4）。
  - connect PASS = OS PC/SC が slot の **ATR を受理**して `SCardConnect` 成功（host は ATR 非依存, §5）。
    FAIL は name 不一致が最多 → 手順1の文字列を再確認。
  - **注**: `poll_interval_ms` はキーに先頭 `_` を付けない（`_pcsc_poll_interval_ms` はコメント扱いで無効）。

## 3. カード UID の登録（`rfid_cards.json`）🖥️

物理カードの UID を読んで `tag_id → card_code` を登録する。未登録の UID は手順4の `watch` に
`card=(未登録)` + 正規化済み UID として出るので、その文字列を控えて登録する:

```bash
python tools/probe_pcsc.py watch --seconds 60   # 各カードを 1 枚ずつタップ → UID を控える
# 控えた UID を rfid_cards.json に追記（例）:
#   "cards": { "04:AB:CD:EF:12:34:56:78": "Ah", ... }
```

- **期待**: タップごとに正規化 UID（`AA:BB:...`）が表示される。
- **見る点**: ISO 15693 は **8 バイト**（`(8B)` 表示）。`⚠ 非契約長` が出る UID は 4/7/8B 以外
  （配線・カード種別を疑う, §7）。`rfid_cards.json` の tag_id も同じ正規化（大文字コロン区切り）で書く。

## 4. ライブ・タップ確認（UID / 役割 / hot-plug, 契約 §6-8）🖥️

```bash
python tools/probe_pcsc.py watch --seconds 30
```

各 slot で「**置く → 離す → もう一度置く**」を行う。

- **期待**: タップごとに `seat N` / `board K` のラベル + 正規化 UID + バイト長 + 解決カードが 1 行出る。
- **見る点**:
  - **役割マッピング**（§4）: 物理リーダー位置と `seat`/`board` ラベルが一致するか。ズレていれば
    `pcsc_readers` の name↔slot 対応を直す（firmware は slot 順序のみ保証、役割は host config が source of truth）。
  - **Get UID**（§6）: タップで毎回 UID が取れる（`FF CA 00 00 00` → UID + `SW=90 00`）。
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

## 受け入れ基準（契約 v1.0 ↔ 本手順）

| 契約 § | 項目 | 確認手段 |
|--------|------|---------|
| §2 | USB CCID class / VID-PID / product 文字列固定 | 手順0（OS 認識）+ 手順1（reader_name に product） |
| §3 | 1 PN5180 = 1 slot / reader_name 安定 | 手順1（再起動跨ぎ） |
| §4 | reader_name↔役割（host config が正準・等値照合） | 手順2 `check`（lint）+ 手順4（役割一致） |
| §5 | PC/SC 互換 ATR で connect 成立 | 手順2 `check`（PASS） |
| §6 | Get UID `FF CA 00 00 00` | 手順4 `watch`（毎タップ UID） |
| §7 | UID 4/7/8B 正規化 | 手順3/4（`(8B)` 表示・`⚠` 無し） |
| §8 | hot-plug / 切断耐性 / multi-platform | 手順4（再発火）+ 手順6（USB 抜き）+ 手順7 |

すべて PASS かつ board street 自動遷移（手順5）まで確認できれば、RFID 実機経路の bring-up 完了。

## 実環境で確定して契約へ追記すべき項目（ISSUE-0015 残）

- firmware の **VID/PID** と各 slot の **実 reader_name** を確定し、`docs/contracts/rfid-usb-ccid.md`
  §2/§4 に追記する（host コードは変更不要 = 契約安定）。
- live hot-add（稼働中の reader 追加追従）は future（本 v1.0 は起動時 connect のみ）。
