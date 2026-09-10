# インストール手順

> 本アプリは現在 **ソースから実行**します（Windows ワンクリックインストーラは準備中）。
> エンジニアでない方は、まず「[1. Python の準備](#1-python-の準備)」と
> 「[2. アプリの取得とインストール](#2-アプリの取得とインストール)」だけ進めれば起動できます。

## 1. Python の準備

- **Python 3.11 以上**をインストールします（[python.org](https://www.python.org/downloads/)）。
- Windows ではインストール時に **「Add Python to PATH」にチェック**を入れてください。
- 確認:

```bash
python --version    # 3.11 以上であること
```

## 2. アプリの取得とインストール

```bash
git clone <このリポジトリ>
cd pokerapp
pip install -r requirements.txt
```

> `pip install .` でも構いません（`pyproject.toml` が正）。

### マイク入力（PortAudio）について

音声入力に使う `pyaudio` は、OS 側の **PortAudio** が必要な場合があります。

| OS | 事前準備 |
|----|----------|
| Windows | 通常は不要（`pip install pyaudio` で wheel が入ります） |
| macOS | `brew install portaudio` の後に `pip install -r requirements.txt` |
| Linux (Debian/Ubuntu) | `sudo apt-get install portaudio19-dev` の後に `pip install -r requirements.txt` |

## 3. 初回起動と音声認識モデル

```bash
python main.py --cli
```

- **初回のみ**、音声認識モデル（既定 `medium`、数百 MB）が自動ダウンロードされます。
  ネット接続のある環境で一度起動しておくと安心です。
- マシンが非力でダウンロードや認識が重い場合は、モデルを小さくできます
  → [設定: `audio.whisper_model`](usage.md#設定-configjson)（例: `small`）。

## 4. マイクの選択

- `config.json`（初回起動で `config_default.json` から自動生成）の `audio.device_id` でマイクを指定します。
- 既定は `0`（システム既定の入力デバイス）。複数マイクがあり別のものを使いたい場合に変更します。
- どの番号がどのマイクか分からない場合は
  [トラブルシューティング: マイクが認識されない](troubleshooting.md#マイクが認識されない録音されない)を参照。

## 5. RFID を使う場合

RFID カード自動認識は店舗運用での中核機能です。**正式（canonical）構成は PC/SC 方式**
（PN5180 NFC リーダー + ESP32-S3 を USB CCID で PC に接続。ADR-0015 / ADR-0034、契約は
`docs/contracts/rfid-usb-ccid.md`）。HTTP 方式は debug / 遠隔の補助用途です。
RFID を使わない場合は `rfid.enabled` を `false`（既定）のままで構いません。

### PC/SC 方式（PN5180 + ESP32-S3 USB CCID、**正式・推奨**）

1. 追加インストール: `pip install ".[pcsc]"`（`pyscard` が入ります）。OS 側に PC/SC スタックが必要です
   （Linux: `sudo apt-get install pcscd libpcsclite-dev` + `pcscd` 起動／macOS・Windows は標準で PC/SC あり）。
2. ESP32-S3 firmware（PN5180 を USB CCID で公開）を接続し、OS が各リーダーを認識していることを確認:
   ```bash
   python -c "from smartcard.System import readers; print([str(r) for r in readers()])"
   ```
   ここに出る **reader_name 文字列を完全一致で** `config.json` の `rfid.pcsc_readers[].name` に記入します
   （OS により文字列が異なります。契約 `docs/contracts/rfid-usb-ccid.md` §4/§8）。
3. `config.json` の `rfid.transport` を `"pcsc"`、`rfid.enabled` を `true` に。
4. `rfid.pcsc_readers`（**list**）で各リーダーの役割を設定:
   `{"name": "<実 reader_name>", "role": "seat", "seat": 1}` / `{"name": "...", "role": "board", "index": 1}`
   （`seat` 1..9 / board は `index` 1..5）。
   本番構成は **11 台**（席 8 台 + board 3 台）。カードは**重ねて置けます**（席 = ホールカード 2 枚、
   board の 1 台目 = フロップ 3 枚）。重ねる board リーダーには枚数 `cards` を付けます:
   `{"name": "...", "role": "board", "index": 1, "cards": 3}`（フロップ）/ `index: 4`（ターン）/
   `index: 5`（リバー）。席リーダーは 2 枚重ねでも `cards` は不要です（契約
   `docs/contracts/rfid-usb-ccid.md` v1.1 §4）。設定の妥当性は
   `python tools/probe_pcsc.py check` で確認できます。
5. カード対応表 `rfid_cards.json`（`tag_id` → カード）を用意（物理カード ↔ UID の登録）。

### HTTP 方式（ESP32 + PN532、補助 / debug 用）

1. `config.json` の `rfid.enabled` を `true`、`rfid.transport` を `"http"` に。
2. `rfid.bind_host` を PC の LAN IP（例 `192.168.1.20`）に変更。既定の `127.0.0.1` は同一 PC 内のみ受信。
   受信は**認証なし**のため、LAN 公開（LAN IP / `0.0.0.0`）は店舗の信頼できる Wi-Fi でのみ行ってください。
3. ESP32 側から読み取り結果を `http://<PCのIP>:8787/`（既定 `8787`）へ POST。
4. `rfid.readers`（**dict**, `seat_1`…`seat_9` / `board_1`…`board_5`）で役割を設定。

> **注**: PC/SC は `pcsc_readers`（list）、HTTP は `readers`（dict）と設定キーが異なります。
> 混同しないでください（ADR-0034）。

詳細な設定項目は [設定リファレンス](usage.md#設定-configjson) を参照してください。

## 次へ

- [使い方ガイド](usage.md) — 読み上げ方とハンド記録の流れ
- [トラブルシューティング](troubleshooting.md) — うまくいかないとき
