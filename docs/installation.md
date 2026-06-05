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

## 5. RFID を使う場合（任意）

RFID は**任意**です。使わない場合は何もしなくて構いません（`rfid.enabled` は既定 `false`）。

### HTTP 方式（ESP32 + PN532、推奨）

1. `config.json` の `rfid.enabled` を `true` に。
2. ESP32 側から、読み取り結果を本アプリの `http://<PCのIP>:8787/`（既定ポート `8787`）へ POST するよう設定。
3. `rfid.readers` で各リーダー（`seat_1`…`seat_9` / `board_1`…`board_5`）の役割を確認・調整。
4. カード対応表 `rfid_cards.json`（`tag_id` → カード）を用意。

### PC/SC 方式（カードリーダー直結）

1. 追加インストール: `pip install ".[pcsc]"`（`pyscard` が入ります）。
2. `config.json` の `rfid.transport` を `"pcsc"` に、`rfid.enabled` を `true` に。

詳細な設定項目は [設定リファレンス](usage.md#設定-configjson) を参照してください。

## 次へ

- [使い方ガイド](usage.md) — 読み上げ方とハンド記録の流れ
- [トラブルシューティング](troubleshooting.md) — うまくいかないとき
