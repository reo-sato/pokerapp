# インストール手順

> **店舗の Windows PC には「[0. Windows ワンステップ](#0-windows-ワンステップ店舗-pc)」だけで入ります**
> （Python が無くても可。ADR-0057）。1 節以降は開発者向け（ソースから手動で入れる手順）です。

## 0. Windows ワンステップ（店舗 PC）

Python が入っていない Windows 10/11 の PC を前提にしています。**ネット接続が必要なのはインストール時だけ**です
（Python・依存パッケージ・音声認識モデルの取得）。

### 手順（店員向け）

1. GitHub の **Code → Download ZIP** でこのリポジトリを取得し、`C:\PokerHandLogger` に展開する
   （フォルダの中に `install.cmd` が見える状態にする）。
2. `install.cmd` を**ダブルクリック**。黒い画面が開いて自動で進みます（数分〜、モデルを含めると 10 分程度）。
   途中で「音声認識モデルを今ダウンロードしますか？」と聞かれたら Enter（= はい）。
3. デスクトップにショートカットが 4 つできれば完了:

| ショートカット | 起動するもの |
|---|---|
| ハンドロガー (CLI) | `main.py --cli --log-file`（ログは `logs\pokerapp.log`） |
| 卓モニタ (iPad から閲覧) | `tools\table_monitor.py --host 0.0.0.0 --port 8790`（画面に PC の IPv4 を表示） |
| 会計 + スマホ注文 API | `main.py --ledger --log-file`（API は `config.json` の `viewer_api.enabled=true` で有効） |
| RFID リーダー チェック | `tools\probe_pcsc.py list` → `check` |

PowerShell を開ける人は、展開の手間なく **1 行**でも入ります（既に入っていれば更新になります）:

```powershell
irm https://raw.githubusercontent.com/reo-sato/pokerapp/verify-v1/installer/bootstrap.ps1 | iex
```

### インストーラがやること（`installer\install.ps1`）

1. **Python 3.12** を探し、無ければ `winget` で導入（winget が無い古い Windows では python.org の
   インストーラをサイレント実行）。3.12 固定なのは PyAudio / pyscard / faster-whisper の Windows wheel が揃う版だから。
2. フォルダ内に `venv` を作り、`pip install -e ".[pcsc,api]"`（RFID の PC/SC と viewer API を含む）。
3. `config.json` を `config_default.json` から生成（既にあれば触らない）。
4. 音声認識モデル（`config.json` の `audio.whisper_model`、既定 `medium` ≈ 1.5 GB）を先読み（任意）。
5. デスクトップにショートカットを作成。
6. 動作確認（主要モジュールの import と `main.py --help`）。
7. 経過は `install.log` に残ります。失敗したときはまずここを見てください。

### 更新 / アンインストール

- **更新**: `update.cmd` をダブルクリック。GitHub の最新（`verify-v1`）を上書き展開します。
  **`config.json` / `rfid_cards.json` / `menu.json` / 会計データ（`*.json`）/ `logs\` / `backups\` は保持**
  （`core/backup.py` のデータ一覧と同じ集合。テストで整合を固定）。削除されたファイルは残ります。
- **アンインストール**: `uninstall.cmd`。ショートカットと `venv` を消し、データは残します。
  完全に消すときはフォルダごと削除。

### 初回起動後にやること

- RFID を使う: `config.json` の `rfid.enabled` を `true`、`rfid.transport` を `"pcsc"` にし、
  「RFID リーダー チェック」で `physical readers: 11` と `check` の PASS を確認（下の 5 節）。
- iPad / スマホから見る: `viewer_api.enabled` を `true`、`viewer_api.bind_host` を `0.0.0.0` に
  （無認証なので**店内の信頼できる Wi-Fi のみ**）。卓モニタは設定不要（ショートカットが LAN 公開で起動）。
- カードの登録: 店舗のデッキは `python tools/register_cards.py run --deck 1` で `rfid_cards.json` に登録
  （同梱のものは開発用デッキ）。

### うまくいかないとき

| 症状 | 対処 |
|---|---|
| 「Python 3.12 を用意できませんでした」 | [python.org](https://www.python.org/downloads/windows/) から 3.12 を入れ（「Add python.exe to PATH」にチェック）、`install.cmd` をもう一度 |
| 「リモート名を解決できませんでした: 'codeload.github.com'」（1 行インストールの取得直後） | 固定 IP にしてデフォルトゲートウェイが空のとき、IPv6 だけ通って GitHub の zip（IPv4 のみ）が取れない。PowerShell で `Get-NetIPConfiguration` を見て `IPv4DefaultGateway` が空なら、ルーターの IP をゲートウェイに設定する（Windows の IP 設定、または管理者 PowerShell で `New-NetRoute -InterfaceAlias "Wi-Fi" -DestinationPrefix 0.0.0.0/0 -NextHop <ルーターの IP>`） |
| winget が `0x8a15005e : The server certificate did not match…` と出して「--source で指定せよ」で止まる | 初期状態の Windows で Microsoft Store ソースの検索が失敗する既知の問題。インストーラは winget のソースを固定し、それでも入らなければ python.org に自動で切り替える（2026-09-24 以降の版）。古い版で止まった場合は `winget install --id Python.Python.3.12 --exact --source winget` を実行してから `install.cmd` をもう一度 |
| pip が失敗する（社内プロキシ等） | `install.log` の URL を確認。プロキシ環境では `HTTPS_PROXY` を設定してから `install.cmd` |
| 起動時に「venv not found」 | `install.cmd` を先に実行する |
| PowerShell の実行ポリシーのエラー | `.cmd` は Bypass 付きで起動するので通常は出ない。出る場合は PowerShell を管理者で開き `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser` |

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
2. ESP32-S3 firmware（PN5180 を USB CCID で公開）を接続し、OS がリーダーを認識していることを確認:
   ```bash
   python tools/probe_pcsc.py list
   ```
   ここに出る **reader_name 文字列を完全一致で** `config.json` の `rfid.pcsc_readers[].name` に記入します
   （OS により文字列が異なります。契約 `docs/contracts/rfid-usb-ccid.md` §4/§8）。
   **リーダー名は 1 個だけ出るのが正常です**（PN5180 が 11 台でも 1 個。Windows の CCID ドライバの
   制限により、物理リーダーは名前ではなく次項の `reader` 番号で選びます。契約 v1.2 / ADR-0052）。
   同じ行に `physical readers: N` として firmware が公開している台数が出ます。
3. `config.json` の `rfid.transport` を `"pcsc"`、`rfid.enabled` を `true` に。
4. `rfid.pcsc_readers`（**list**）で各リーダーの役割を設定:
   `{"name": "<実 reader_name>", "reader": 0, "role": "seat", "seat": 1}` /
   `{"name": "<同じ実 reader_name>", "reader": 8, "role": "board", "index": 1, "cards": 3}`
   （`reader` = 物理リーダー番号 0 起点 / `seat` 1..9 / board は `index` 1..5）。
   本番構成は **物理 11 台**（席 8 台 = `reader` 0..7 + board 3 台 = `reader` 8..10）で、
   **`name` は全要素で同じ文字列**、`reader` だけが違います（`(name, reader)` の組が一意）。
   カードは**重ねて置けます**（席 = ホールカード 2 枚、board の 1 台目 = フロップ 3 枚）。
   重ねる board リーダーには枚数 `cards` を付けます: `"index": 1, "cards": 3`（フロップ）/
   `"index": 4`（ターン）/ `"index": 5`（リバー）。席リーダーは 2 枚重ねでも `cards` は不要です
   （契約 `docs/contracts/rfid-usb-ccid.md` v1.1 §4 / v1.2 §4）。設定の妥当性は
   `python tools/probe_pcsc.py check` で確認できます（`reader` が firmware の台数を超えていると
   `SW=6A86` で FAIL）。同梱の `config_default.json` に 11 台ぶんのサンプルがあります。
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
