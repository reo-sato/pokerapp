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
3. デスクトップにショートカットができれば完了:

| ショートカット | 起動するもの |
|---|---|
| ハンドロガー (CLI) | `start_logger.cmd` → `main.py --cli --log-file`（ログは `logs\pokerapp.log`） |
| 卓モニタ (iPad から閲覧) | `start_monitor.cmd` → `tools\table_monitor.py --host 0.0.0.0 --port 8790`（画面に PC の IPv4 を表示） |
| お客さん用 ハンド履歴 (スマホ) | `start_viewer.cmd` → `main.py --viewer-api --host 0.0.0.0 --port 8788`（お客さんが `http://<PC の IPv4>:8788/` を開く） |
| 真のアクション入力 (iPad から) | `start_truth.cmd` → `tools\ground_truth_ui.py --host 0.0.0.0 --port 8791`（ハンドごとに実際のアクション列を入れる = `docs/usage.md`。既に動いていれば二重に起動せず知らせる） |
| 台本のハンド (声だけ) | `start_script.cmd` → `main.py --cli --log-file --script voice`（声だけの台本。RFID は使わない。台本は真のアクション入力の画面の「台本」に出る。真のアクション入力が動いていなければ別の窓で一緒に起動） |
| 札の確認 (RFID) | `start_cards.cmd` → `main.py --cli --log-file --script cards`（札の確認の台本 = 席 4・5・6。真のアクション入力は同上） |
| ログをまとめる (送付用) | `pack_logs.cmd` → `tools\pack_logs.py`（直近 12 時間のテストのログをデスクトップの zip 1 つに = レビューに送る。音声は合計 25 MB までなら入る） |
| ログをまとめる (音声付き・送付用) | `pack_logs_audio.cmd` → `tools\pack_logs.py --audio`（発話の音声も全部。25 MB を超えると `_1of3.zip` のように分かれるので全部送る） |
| 会計 + スマホ注文 API | `start_ledger.cmd` → `main.py --ledger --log-file`（API は `config.json` の `viewer_api.enabled=true` で有効。スタッフの iPad は `http://<PC の IPv4>:8788/staff/` = `docs/usage.md`） |
| 店舗サーバー 自動起動の登録 | `server_autostart.cmd` → `installer\store_server_task.ps1`（お客さん・iPad の画面と注文・卓モニタをサインイン時に自動で動かす = `docs/usage.md`「店舗サーバー」） |
| RFID リーダー チェック | `rfid_check.cmd` → `tools\probe_pcsc.py list` → `check`（RFID の中継が動いていれば中継を通して確かめる） |

PowerShell を開ける人は、展開の手間なく **1 行**でも入ります（既に入っていれば更新になります）:

```powershell
irm https://raw.githubusercontent.com/reo-sato/pokerapp/verify-v1/installer/bootstrap.ps1 | iex
```

### インストーラがやること（`installer\install.ps1`）

1. **Python 3.12** を探し、無ければ `winget` で導入（winget が無い古い Windows では python.org の
   インストーラをサイレント実行）。3.12 固定なのは PyAudio / pyscard / faster-whisper の Windows wheel が揃う版だから。
2. フォルダ内に `venv` を作り、`pip install -e ".[pcsc,api]"`（RFID の PC/SC と viewer API を含む）。
   続けて第 2 の耳（日本語専用の音声認識）の部品 `pip install -e ".[ear]"` を入れる（入らなくても止めない。本体は動く）。
3. `config.json` を `config_default.json` から生成（既にあれば触らない）。
4. 音声認識モデル（`config.json` の `audio.whisper_model`、既定 `medium` ≈ 1.5 GB）を先読み（任意）。
   第 2 の耳のモデル（約 170 MB。GitHub の配布物 約 713 MB を読みながら取り出す）を `models\` に取得
   （`tools\second_ear.py --model-only`。取得済みなら何もしない。取れなくても止めず、Whisper だけで聞き取る。次の更新でもう一度試す）。
5. デスクトップにショートカット（上の 11 個）を作成。
6. 動作確認（主要モジュールの import と `main.py --help`）。
7. 経過は `install.log` に残ります。失敗したときはまずここを見てください。

### 更新 / アンインストール

- **更新**: `update.cmd` をダブルクリック。GitHub の最新（覚えたブランチ。既定は開発版 `verify-v1`）を上書き展開します。
  **リリース版**（店で確かめた版だけ = `stable`, ADR-0066）にするときは、一度だけ PowerShell で
  `C:\PokerHandLogger\update.cmd -Branch stable` を実行します（以後の `update.cmd` は `stable` から取ります。
  開発版に戻すときは `-Branch verify-v1`）。いま入っている版は `.\venv\Scripts\python.exe main.py --version`
  か、ブラウザで `http://<PC の IPv4>:8788/api/health` で見られます。
  **`config.json` / `rfid_cards.json` / `menu.json` / 会計データ（`*.json`）/ `logs\` / `backups\` / `models\`
  （第 2 の耳のモデル）は保持**（`core/backup.py` のデータ一覧と同じ集合。テストで整合を固定）。削除されたファイルは残ります。
  更新は管理者の権限なしで動くので、**RFID の中継（下の「店舗 PC を画面なし・RDP で運用する場合」）は古い版のまま動き続けます**。
  中継を新しい版にするには、更新のあと管理者で `installer\rfid_relay_task.ps1` をもう一度実行します。
- **アンインストール**: `uninstall.cmd`。ショートカット・店舗サーバーと RFID の中継のタスク・`venv` を消し、データは残します
  （中継のタスクを消すには管理者の権限が要ります。足りなければ消し方を出して止まります）。
  完全に消すときはフォルダごと削除。

### 初回起動後にやること

- RFID を使う: `config.json` の `rfid.enabled` を `true`、`rfid.transport` を `"pcsc"` にし、
  「RFID リーダー チェック」で `physical readers: 11` と `check` の PASS を確認（下の 5 節。中継を通すときは
  「中継: 動いています（リーダー 11/11 台）」と PASS）。
  **RDP で操作する店舗 PC では、先に RFID の中継を登録する**（RDP の中からはリーダーが見えない。下の「店舗 PC を画面なし・
  RDP で運用する場合」の 3）。中継が動いていれば「RFID リーダー チェック」も中継を通して確かめます。
- マイクを名前で固定する: `.\venv\Scripts\python.exe tools\audio_check.py list` で卓のマイクの名前を見て、
  `.\venv\Scripts\python.exe tools\set_config.py audio.device_name "Wireless Mic Rx|DJI Mic Mini 2"`
  （名前の一部、「|」で優先順の候補。番号 `audio.device_id` は Bluetooth のつなぎ外しでずれる。
  [トラブルシューティング: マイク](troubleshooting.md#マイクが認識されない録音されない)）。
- お客さんがスマホで自分のハンドを見る: ハンドロガーが席とお客さんを記録するよう
  `.\venv\Scripts\python.exe tools\set_config.py session_layer.enabled true`（インストール先のフォルダで）。
  画面は「お客さん用 ハンド履歴 (スマホ)」で出ます（config の変更は不要。使い方は `docs/usage.md`）。
- 会計画面からスマホ注文も受ける: `viewer_api.enabled` を `true`、`viewer_api.bind_host` を `0.0.0.0` に
  （無認証なので**店内の信頼できる Wi-Fi のみ**）。卓モニタは設定不要（ショートカットが LAN 公開で起動）。
- カードの登録: 店舗のデッキは `python tools/register_cards.py run --deck 1` で `rfid_cards.json` に登録
  （同梱のものは開発用デッキ）。スートを取り違えて登録した（新品のデッキの並びが登録の順 ♠♥♦♣ と違った など）ときは、
  52 枚を登録し直さずに `python tools/register_cards.py swap-suits d c`（ダイヤとクラブを入れ替える。任意の 2 つのスート。
  前の登録はバックアップに残り、同じコマンドをもう一度で元に戻る）。確かめるには A♦ を置いて `Ad` と出るか
  （`python tools/rfid_relay.py watch`、中継を使っていなければ `python tools/probe_pcsc.py watch`）。
- 読み取り装置（ESP32）のファームウェアの書き換え（使えるリーダーの一覧・ロガーからの再起動 `rr` を使うには
  2026-10-06 の版が要る）: 店舗 PC に ESP-IDF v5.3.5 を入れ、「ESP-IDF 5.3 CMD」で 1 行のコマンドを実行する。手順は
  `firmware\esp32s3-pn5180-ccid\README.md` の「書き換え（店舗 PC に ESP-IDF がある場合…）」（部品の版は
  esp_tinyusb 1.7.6・tinyusb 0.19 に固定）。

### 店舗 PC を画面なし・RDP で運用する場合

2026-09-24 の店舗 PC（Windows 11 Pro、HDMI ダミープラグでヘッドレス、RDP で操作）で必要だった設定。
コマンドは PowerShell にそのまま貼る。管理者の確認には「はい」。

1. **卓の USB は基板の「USB」口**（ESP32-S3 直結 = カードリーダーとして見える）を PC に**直接**挿す。
   「UART」口（CP2102N = COM ポート）はファームウェアの書き込みとログ用で、本番は外してよい。
   どちらがどちらか分からないときは [実機 RFID QA チェックリスト](hardware-qa-checklist.md) §0 の見分け方。
   ハブを使うならキーボードとマウスだけにする（RFID をハブ経由にすると瞬断・電流不足のおそれ）。
   **UART 側を外したら、11 台すべてにカードを置いて反応を確かめる**
   （`python tools/probe_pcsc.py watch --seconds 120`。RDP では 3 の中継を登録してから。中継が動いていれば中継を通す）。
   電源不足で起動しなかったリーダーは PC からは「カードなし」に見えるので、`check` の PASS だけでは見分けられない。
   店の卓の ESP32 は給電アダプタから電源を取るので、**PC との USB を抜き挿ししても ESP32 は再起動しない**。
   リーダーが読めないときは給電アダプタを抜き挿しする（[トラブルシューティング: RFID](troubleshooting.md#一部のリーダーボードの真ん中などだけ読めない)）。
2. **RDP のスマートカード転送を止める**。RDP は既定で、PC に挿したリーダーではなく接続元の端末の
   リーダーをセッション内のアプリに見せる。止めたあと、RDP を一度閉じて接続し直すと効く。

   ```powershell
   Start-Process reg.exe -Verb RunAs -Wait -ArgumentList 'add "HKLM\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services" /v fEnableSmartCard /t REG_DWORD /d 0 /f'; reg query "HKLM\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services" /v fEnableSmartCard
   ```

3. **RFID の中継を登録する**。2026-09-30 の店舗 PC では、2 を済ませても RDP のセッションの中のアプリからリーダーが
   見えなかった（`0x8010001D`「スマート カード リソース マネージャーが実行されていません」。サインアウトしても同じ）。
   システムの権限からは 11 台すべて読めたので、スケジュールタスク **`PokerRFIDRelay`**（システムの権限・PC の起動時に
   自動で動く）でリーダーを読み、PC の中（`127.0.0.1:8792`, config `rfid.relay_port`。外からは受け付けないので
   ファイアウォールの設定は要らない）だけに渡す。ロガー・「RFID リーダー チェック」・`register_cards run` は、中継が
   動いていれば中継から札を受け取る（無ければこれまでどおり直接読む）。一度だけ実行する:

   ```powershell
   Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File C:\PokerHandLogger\installer\rfid_relay_task.ps1'
   ```

   登録するとすぐ起動し、リーダーごとの読み取り（`python tools/rfid_relay.py status --wait 20` と同じ）を出す。
   ログは `logs\rfid_relay.log`。確かめるときは `python tools/rfid_relay.py status` / `python tools/rfid_relay.py watch --seconds 60`。
   **アプリを更新したら、同じコマンドをもう一度実行する**（更新は動いている中継を止めないので、古い版のまま動く）。
   消すときは末尾に `-Remove` を付ける:

   ```powershell
   Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File C:\PokerHandLogger\installer\rfid_relay_task.ps1 -Remove'
   ```

4. **PC のマイクを使えるようにする**。接続元のリモートデスクトップの設定で、音声の再生を「リモート コンピューターで
   再生する」（Mac / iPad の Windows App は「リモート PC で再生」）にしてつなぎ直す（既定では音声が接続元の端末に回り、
   PC に挿したマイクが WDM-KS 方式にしか見えず開けない）。詳しくは
   [トラブルシューティング: マイク](troubleshooting.md#マイクが認識されない録音されない)。
5. **iPad / スマホから卓モニタと API を見られるようにする**。店の Wi-Fi を「プライベート」にし、
   同じ店内ネットワークからだけ 8788（viewer API）・8790（卓モニタ）・8791（真のアクション入力）を受け付ける。

   ```powershell
   Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -Command "Set-NetConnectionProfile -InterfaceAlias ''Wi-Fi'' -NetworkCategory Private; New-NetFirewallRule -DisplayName ''Poker Hand Logger LAN'' -Direction Inbound -Protocol TCP -LocalPort 8788,8790,8791 -RemoteAddress LocalSubnet -Action Allow -Profile Private | Out-Null; Get-NetConnectionProfile | Format-Table Name, InterfaceAlias, NetworkCategory -AutoSize; Get-NetFirewallRule -DisplayName ''Poker Hand Logger LAN'' | Format-Table DisplayName, Enabled, Profile, Action -AutoSize; pause"'
   ```

   初回起動時に Windows セキュリティの警告が出たら「アクセスを許可する」（キャンセルすると
   ブロック規則が作られ、上の許可より優先される）。
6. **固定 IP にするならデフォルトゲートウェイも入れる**（空だと IPv6 だけ通り、GitHub からの取得や
   更新が失敗する。下のトラブル表）。ルーター側での DHCP 予約のほうが、他の機器と番号がぶつからず安全。
7. **RDP は右上の × で閉じる**（切断）。起動したハンドロガーと卓モニタは動き続ける。サインアウトすると止まる。

### うまくいかないとき

| 症状 | 対処 |
|---|---|
| 「Python 3.12 を用意できませんでした」 | [python.org](https://www.python.org/downloads/windows/) から 3.12 を入れ（「Add python.exe to PATH」にチェック）、`install.cmd` をもう一度 |
| 「リモート名を解決できませんでした: 'codeload.github.com'」（1 行インストールの取得直後） | 固定 IP にしてデフォルトゲートウェイが空のとき、IPv6 だけ通って GitHub の zip（IPv4 のみ）が取れない。PowerShell で `Get-NetIPConfiguration` を見て `IPv4DefaultGateway` が空なら、ルーターの IP をゲートウェイに設定する（Windows の IP 設定、または管理者 PowerShell で `New-NetRoute -InterfaceAlias "Wi-Fi" -DestinationPrefix 0.0.0.0/0 -NextHop <ルーターの IP>`） |
| winget が `0x8a15005e : The server certificate did not match…` と出して「--source で指定せよ」で止まる | 初期状態の Windows で Microsoft Store ソースの検索が失敗する既知の問題。インストーラは winget のソースを固定し、それでも入らなければ python.org に自動で切り替える（2026-09-24 以降の版）。古い版で止まった場合は `winget install --id Python.Python.3.12 --exact --source winget` を実行してから `install.cmd` をもう一度 |
| ウイルス対策ソフト（ノートンなど）が `install.ps1` を「IDP.Generic」等で止めた（ふるまい検知） | 誤検知（Python の取得・venv の作成・自分の起動し直しが型に似ている）。ソフトの「除外」に `C:\PokerHandLogger` を足して 1 行インストールをやり直すか、真のアクションを家で入れるだけなら手で入れる: python.org から 3.12（「Add python.exe to PATH」）→ `cd C:\PokerHandLogger; py -3.12 -m venv venv; venv\Scripts\python.exe -m pip install numpy "pokerkit>=0.7,<0.8"` → `home_truth.cmd` |
| pip が失敗する（社内プロキシ等） | `install.log` の URL を確認。プロキシ環境では `HTTPS_PROXY` を設定してから `install.cmd` |
| 起動時に「venv not found」 | `install.cmd` を先に実行する |
| 「第 2 の耳のモデルを取得できませんでした（Whisper だけで聞き取ります…）」 | 止まらずに続く（本体は Whisper だけで聞き取る）。次の更新でもう一度取りに行く。すぐ取るなら `.\venv\Scripts\python.exe tools\second_ear.py --model-only` |
| アンインストールで「RFID の中継のタスクを消せません（管理者の権限が要ります）」 | 表示された `Start-Process powershell -Verb RunAs … rfid_relay_task.ps1 -Remove` を PowerShell に貼ってから、`uninstall.cmd` をもう一度 |
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

- **初回のみ**、音声認識モデル（既定 `medium`、約 1.5 GB）が自動ダウンロードされます。
  ネット接続のある環境で一度起動しておくと安心です。
- マシンが非力でダウンロードや認識が重い場合は、モデルを小さくできます
  → [設定: `audio.whisper_model`](usage.md#設定-configjson)（例: `small`。精度は下がる）。
- 第 2 の耳を使うなら `pip install -e ".[ear]"` と `python tools/second_ear.py --model-only`（約 170 MB を `models/` に）。
  無くても Whisper だけで動きます。

## 4. マイクの選択

- `config.json`（初回起動で `config_default.json` から自動生成）の **`audio.device_name`** にマイクの名前（の一部）を
  書くと、起動のたびにその名前のマイクを探して開きます（「|」で区切ると優先順の候補。例 `Wireless Mic Rx|DJI Mic Mini 2`）。
  Bluetooth のマイクのつなぎ外しで番号がずれても同じマイクを開きます。どの名前も見つからなければマイクを開きません。
- `audio.device_name` が空なら `audio.device_id`（番号）で選びます。既定は `0`。番号は起動のたびの並びで決まるので、
  ずれることがあります。
- マイクの名前・番号は `python tools/audio_check.py list` で見られます。詳しくは
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
   `{"name": "<同じ実 reader_name>", "reader": 8, "role": "board"}`
   （`reader` = 物理リーダー番号 0 起点 / `seat` 1..9 / board は役割だけ）。
   本番構成は **物理 11 台**（席 8 台 = `reader` 0..7 + board 3 台 = `reader` 8..10）で、
   **`name` は全要素で同じ文字列**、`reader` だけが違います（`(name, reader)` の組が一意）。
   カードは**重ねて置けます**（席 = ホールカード 2 枚、board の 1 台に 1〜3 枚）。
   board のリーダーは全台で 1 つのボードとして扱い、何枚目かは配った順で決まります。board のリーダーは
   **左から右の順に並べて書きます**。旧 config の `index` / `cards` は廃止です（残っていても無視し、起動時に警告）
   （契約 `docs/contracts/rfid-usb-ccid.md` §4, ADR-0053）。設定の妥当性は
   `python tools/probe_pcsc.py check` で確認できます（`reader` が firmware の台数を超えていると
   `SW=6A86` で FAIL）。同梱の `config_default.json` に 11 台ぶんのサンプルがあります。
5. カード対応表 `rfid_cards.json`（`tag_id` → カード）を用意（物理カード ↔ UID の登録 = `python tools/register_cards.py run --deck 1`）。
6. RDP で操作する PC では、RDP の中からリーダーが見えないことがあります → RFID の中継
   （[店舗 PC を画面なし・RDP で運用する場合](#店舗-pc-を画面なしrdp-で運用する場合) の 3）。

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
