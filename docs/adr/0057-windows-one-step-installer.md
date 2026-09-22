# ADR-0057: 配布形態 — Windows ワンステップインストーラ（フォルダ in-place + venv、スクリプト方式）

## Status

Accepted（2026-09-22, Stage 1 実装済）

## Date

2026-09-22

## Context

これまでの実機テストは開発者の PC（Python が複数入っている。ESP-IDF の Python が `python` を
奪っていて pytest が無い、といった衝突も実際に起きた）で行ってきた。本番は**店舗のノート PC**で、
Python も git も無い前提。オーナーは製品化を見据えて「一手でインストールできる」ことを求めている。

現行コードの前提で配布形を縛るものが 3 つある。

1. **データファイルの置き場がアプリのフォルダ直下**: `config.json` / `players.json` / `sessions.json` /
   `ledger.json` / `order_requests.json` / `player_credentials.json` / `auth_identity.json` /
   `rfid_cards.json` / `menu.json` / `logs/` / `backups/` はすべて `Path(__file__).parent.parent` か
   cwd 相対で解決される。`pip install .` で site-packages に入れると、書き込み先が site-packages になる。
2. **iPad / スマホ画面（`staff/` `mobile/`）は Expo の web export（`dist/`）を「任意の静的サーバーで
   LAN 配信」**する前提 = 店舗 PC に Node.js が要る。
3. **音声認識モデル**（faster-whisper `medium` ≈ 1.5 GB）は初回起動時に HuggingFace から取得する。

## Decision

### Stage 1（本 ADR で実装）: スクリプト方式の一手インストール

- **配布単位は「フォルダ」**。GitHub の zip（または 1 行 bootstrap）で `C:\PokerHandLogger` に展開し、
  `install.cmd` をダブルクリックする。`installer/install.ps1` が
  1. **Python 3.12** を確保する（`py -3.12` → 既知パス → `python` の順に探し、無ければ `winget` →
     python.org のサイレントインストール）。**3.12 固定**なのは PyAudio / pyscard / ctranslate2 の
     Windows wheel が揃っている版だから（3.13/3.14 は wheel 欠けでビルドに落ちる）。
  2. **フォルダ内に `venv`** を作り `pip install -e ".[pcsc,api]"`（editable）。site-packages には
     入れない = 上記 1 の前提を変えずに済む。
  3. `config.json` を雛形から生成（既存は不変）、Whisper モデルの先読み（任意・質問する）、
     デスクトップにショートカット 4 つ、import と `main.py --help` で動作確認、`install.log`。
- **更新** `update.cmd`: GitHub の zip を `robocopy /E`（追加・上書きのみ）で重ねる。
  **店舗固有のもの = `core/backup.py` のデータ一覧 + `config.json` / `rfid_cards.json` / `menu.json` と
  `venv` / `logs` / `backups`** を除外する（`tests/test_installer.py` が両者の整合を固定）。
  `.git` があり git が使えるなら `git pull --ff-only`。
- **アンインストール** `uninstall.cmd`: ショートカットと `venv` だけ消す（データは残す）。
- **1 行版** `installer/bootstrap.ps1`: `irm …/bootstrap.ps1 | iex`。**ユーザーの対話コンソールの中で
  実行される**ので書き方に制約がある: param は使えず環境変数で受ける / **`exit` を書かない**（iex の中の
  exit はユーザーの PowerShell ウィンドウごと閉じ、結果が読めなくなる）/ **BOM を付けない**（5.1 では
  irm の戻り値の先頭に U+FEFF が残り得て iex が失敗する）/ 全体を `& { }` で包む。既に展開済みなら
  更新モードに落とし、取得した zip にインストーラが無ければ（ブランチ違い）明示エラーにする。
  `.ps1` の直接実行は実行ポリシーに掛かるので、内部で `powershell -ExecutionPolicy Bypass -File` を
  使う（`.cmd` も同様）。
- **エンコーディング規約**: `-File` で実行する `install.ps1` は **UTF-8 BOM**（Windows PowerShell 5.1 は
  BOM 無しを ANSI として読み日本語が化ける）、`bootstrap.ps1` は上記のとおり **BOM 無し**、`.cmd` は
  **ASCII のみ + CRLF**（日本語 Windows の cmd.exe は CP932 で読む。日本語の案内は .ps1 側に置く）。
  `.gitattributes` で CRLF を固定。`tests/test_installer.py` が全部を固定する。
- **ランチャ**は `chcp 65001` + `PYTHONUTF8=1` で UTF-8 コンソールにし、hand logger は必ず
  `--log-file`（RFID ログの割り込み対策, ISSUE-0034）。

### Stage 2（未着手・別 ADR）

- iPad / スマホ画面の `dist/` を CI でビルドし **API サーバーから配信**（店舗 PC に Node 不要）。
- **データフォルダの分離**（`C:\PokerHandLogger\data` 等）→ アプリを丸ごと差し替え可能に。
- GitHub Actions の release で zip を自動生成 → **PyInstaller + Inno Setup の `Setup.exe`**
  （Python 不要・オフライン可・署名）。
- 自動起動（サービス化 / スタートアップ）、ログローテーション。

## Alternatives considered

- **`pip install pokerapp` を site-packages に**: データの書き込み先が壊れる（上記 1）。却下。
- **最初から PyInstaller + Inno Setup**: ctranslate2 / customtkinter / tkinter / pyscard の同梱が
  手間で壊れやすく、Windows のビルド環境（CI runner）が要る。店舗 1 台の dogfood 段階では過剰。
  Stage 2 に回す。
- **uv / pipx**: 結局 uv の導入が 1 手増え、データ置き場の問題も解けない。
- **Node を店舗 PC に入れて iPad 画面も同梱**: 実プレイ環境テストでは Python 側の卓モニタしか
  使わないため、オーナー判断で Stage 1 の範囲外。

## Consequences

- 店員の操作は「zip を展開 → `install.cmd`」または「PowerShell に 1 行」。Python の知識は不要。
- インストール時だけネット接続が要る（Python / pip / モデル）。以後はオフラインで動く。
- **フォルダがインストールの単位**なので、フォルダを動かすと venv のパスが壊れる（再 `install.cmd` で復旧）。
- `update.cmd` は削除を行わないため、上流で消えたファイルが残る（Stage 2 のデータ分離で `/MIR` 化）。
- 開発環境（Linux CI / この会話の実行環境）では Windows 固有部分（winget / WScript.Shell / robocopy）を
  実行できない。**構文解析と `-DryRun` の通し**を CI（ubuntu の pwsh）で固定し、実 Windows での通しは
  オーナーの PC で行う（`docs/worklog/2026-09-22-windows-one-step-installer.md`）。

## Related

- ADR-0015（RFID canonical = PC/SC）/ ADR-0052（1 slot + P2）/ ISSUE-0034（`--log-file`）
- `docs/installation.md` §0 / `tests/test_installer.py` / `core/backup.py`
