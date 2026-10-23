# Worklog: Windows ワンステップインストーラ（店舗 PC 向け, Stage 1）

## Date

2026-09-22

## Scope / Task

製品化を見据え、Python の無い店舗のノート PC に **一手でインストール**できるようにする（ADR-0057 Stage 1）。
iPad / スマホ画面（Node が要る）の同梱はオーナー判断で範囲外（Stage 2）。

## Goal

- 店員の操作: zip を `C:\PokerHandLogger` に展開 → `install.cmd` をダブルクリック（または PowerShell に 1 行）。
- デスクトップのショートカットから hand logger / 卓モニタ / 会計 + API / RFID チェックを起動できる。
- 更新で店舗固有のデータ（config / rfid_cards / 会計 JSON / logs）を消さない。
- Windows を持たない CI でも壊れを検知できる（構文解析 + DryRun）。

## Changed Files

- `installer/install.ps1` — **新規**。本体（UTF-8 BOM, PowerShell 5.1 互換）。Python 3.12 確保 / venv /
  `pip install -e ".[pcsc,api]"` / config 生成 / モデル先読み / ショートカット / 動作確認 / `-Update`（zip 上書き,
  保持リスト）/ `-Uninstall` / `-DryRun` / `install.log`。
- `installer/bootstrap.ps1` — **新規**。`irm … | iex` の 1 行インストール（zip 展開 → install.ps1）。
- `install.cmd` / `update.cmd` / `uninstall.cmd` — **新規**。ダブルクリック用の入口（ASCII + CRLF、
  `-ExecutionPolicy Bypass`）。
- `start_logger.cmd` / `start_monitor.cmd` / `start_ledger.cmd` / `rfid_check.cmd` — **新規**。ランチャ
  （venv 経由、`chcp 65001` + `PYTHONUTF8=1`、hand logger は `--log-file`）。
- `.gitattributes` — **新規**。`*.cmd` / `*.ps1` を CRLF 固定。
- `.gitignore` — `venv/` / `install.log`。
- `pyproject.toml` — `[tool.setuptools] packages` に `api` を追加（`pip install .` で viewer API が抜けていた）。
- `tests/test_installer.py` — **新規**（30 件）。
- docs: `docs/installation.md` §0 / `README.md` / `docs/adr/0057-…md` / `CLAUDE.md`（ディレクトリ構成・実装状況・
  コマンド）/ `CHANGELOG.md` / `docs/decision-log.md`。

## Expected Behavior

- 何も入っていない Windows 10/11 で `install.cmd` 1 回で起動可能になる（インストール時のみネット接続）。
- 既にインストール済みなら再実行は冪等（venv / config.json を壊さない）。
- `update.cmd` は保持リストのものを一切変えない。

## Implemented Behavior

上記のとおり。判断した点:

1. **フォルダ in-place + editable**。データが `Path(__file__).parent.parent` 直下にある現行設計を変えずに
   済ませる。site-packages には入れない。
2. **Python 3.12 固定**。開発 PC で見た「py 3.14 には pyscard の wheel が無い / 3.13 には pytest が無い」の
   再発を避け、PyAudio / pyscard / ctranslate2 の wheel が揃う版に寄せる。ESP-IDF の python や
   Microsoft Store のスタブ（exit 9009）は `sys.version_info` の probe で弾く。
3. **エンコーディング**: `-File` で実行する `install.ps1` は BOM 付き UTF-8（5.1 対策）、`.cmd` は ASCII のみ
   （CP932 対策）。日本語の案内は .ps1 側。**`bootstrap.ps1` は逆に BOM 無し + `exit` 無し + `& { }` 包み**
   （`irm … | iex` でユーザーの対話コンソールの中で走るため。下の Mismatches 参照）。
4. **stderr の扱い**: 5.1 では `2>&1` + `$ErrorActionPreference=Stop` でネイティブコマンドの stderr が
   例外になる（pip の progress で落ちる）ので、`Invoke-Checked` は stderr を端末に流し exit code だけ見る。
5. **保持リストの整合をテストで固定**: `$PreservedFiles` ⊇ `core/backup.py` の `_ROOT / "*.json"`。
6. **DryRun** は Linux の pwsh でも全手順を通る（Windows 固有 API は DryRun で触らない）。

## Test Results

- `pytest tests/test_installer.py`（pwsh あり）— **30 passed**（構文解析 2 + DryRun 通し 1 を含む）。
- `pytest tests/ -q --ignore=tests/test_vision.py` — pwsh あり **1178 passed** / pwsh 無し **1175 passed,
  3 skipped**（skipped = PowerShell 検査 3 件。CI の ubuntu ランナーには pwsh があるので実行される）。
- `ruff check .` — clean。GitHub Actions CI（ubuntu, pwsh あり）は最初のコミット `467f635` で **success**
  （run #242）。
- Windows PowerShell 5.1 向けの静的確認（Linux の pwsh 7 では検出できないもの）: PS7 専用構文
  （`??` / `?.` / 三項 / `$IsWindows` / `-LeafBase` / `GetRelativePath`）を含まない、`Join-Path` は
  すべて 2 引数、`Invoke-WebRequest` は全箇所 `-UseBasicParsing`、TLS 1.2 を明示、リポジトリのパスは
  すべて ASCII（最長 87 文字 = `Expand-Archive` の MAX_PATH 内）、`pyproject.toml` に `[build-system]`
  あり（3.12 の venv は setuptools を同梱しないが isolated build が取得する）。
- **実 Windows での通しは未実施**（この実行環境は Linux）。オーナーの PC での手順は下記。

## Mismatches Found During Testing

- ランチャ検査の正規表現が `if not exist "venv\Scripts\python.exe" (` の行も拾って `(` をパス扱いしていた
  → 行頭アンカーで修正。
- Linux の pwsh で `-DryRun` が `Join-Path $env:LOCALAPPDATA` の null で落ちた（Windows では常に存在）
  → 環境変数が無ければ候補に入れないよう修正（Windows でも無害）。
- コミット後の机上レビュー（`irm … | iex` の実行モデル = ユーザーの対話コンソールの中で走る）で 3 件:
  - `bootstrap.ps1` が `exit $LASTEXITCODE` で終わっていた → iex の中の `exit` は**ユーザーの PowerShell
    ウィンドウごと閉じる**ので「完了」もエラーも読めない → `exit` を全廃し、失敗は赤字表示にして自然終了。
    全体を `& { }` で包み、変数・`$ErrorActionPreference` をセッションに残さない。
  - `bootstrap.ps1` に UTF-8 BOM を付けていた → 5.1 の `irm` は戻り値の先頭に U+FEFF を残し得て `iex` が
    失敗する（pwsh 7 でも `iex ([string][char]0xFEFF + "Write-Output ok")` は
    `The term '﻿Write-Output' is not recognized` で失敗した = irm が BOM を落とさない環境では確実に壊れる）
    → **BOM 無し**に変更。
    `install.ps1`（`-File` 実行）は BOM 付きのまま。テストを「install.ps1 = BOM 有 / bootstrap.ps1 = BOM 無 +
    exit 無」に分けた。
  - 本 worklog の検証 one-liner が `$env:POKERAPP_BRANCH` を設定しておらず、既定の `verify-v1` の zip
    （インストーラ未収載）を取りに行って失敗する → one-liner を修正 + zip に `installer\install.ps1` が
    無ければ「ブランチ違い」を明示する throw を追加。

## Fixes Applied

上記 5 件。

## 実機導入（店舗 PC, 2026-09-24）

GEEKOM A8（Windows 11 Pro、初期セットアップ直後・ローカルアカウント・RDP でヘッドレス運用）に 1 行インストールで
導入した。依頼元の手順書（Windows 初期設定 / RDP / 固定 IP / BIOS / ヘッドレス化）を 5 まで済ませた状態から。

1. **取得直後に「リモート名を解決できませんでした: 'codeload.github.com'」**。原因はアプリではなく、手順書 3.2 の
   固定 IP で **デフォルトゲートウェイが空**だったこと（`IPv4=192.168.1.136/24 GW4=` / DHCP 無効 / DNS はルーターの
   IPv6 アドレスのみ。手順書の 8.8.8.8 / 1.1.1.1 は反映されていなかった）。IPv6 はルーターの RA で生きていたので、
   IPv6 を持つ raw.githubusercontent.com（1 行目）は取れ、IPv4 しか持たない codeload だけが落ちた。1.1.1.1 宛ては
   `WSAENETUNREACH`（10051）= IPv4 の経路なし。ルーター 192.168.1.1 は arp 表と、IPv6 DNS アドレスの EUI-64
   （`0225:36ff:fe5d:3310` ← MAC `00-25-36-5d-33-10`）が一致したことで特定。管理者で
   `New-NetRoute -InterfaceAlias Wi-Fi -DestinationPrefix 0.0.0.0/0 -NextHop 192.168.1.1`（IP は変えないので RDP は
   切れない）→ 解消。**アプリ側の対応**: bootstrap の取得失敗時に「固定 IP ならゲートウェイを確認」と案内する。
   installation.md §0 のトラブル表に追加。
2. **winget が `0x8a15005e : The server certificate did not match any of the expected values` で msstore ソースの
   検索に失敗し、「--source で指定せよ」と言って何も入れずに終了**。旧実装は winget があれば python.org に
   フォールバックしなかったので、「Python 3.12 を用意できませんでした」で止まった。**修正**: winget を
   `--source winget` に固定 / 成否は Find-Python で判定し、入らなければ python.org のサイレント導入に自動で
   切り替える / python.org 版は `InstallLauncherAllUsers=0`（per-user 導入でランチャが昇格を求めない）。
3. 付随の改善: **`-Update` は更新後のインストーラで続きを実行し直す**（旧実装はファイルを差し替えても、実行中の
   古いスクリプトで Python / 依存の手順を続けていた = 上の修正が次回まで効かない）。`$ProgressPreference =
   "SilentlyContinue"`（5.1 の Invoke-WebRequest は進捗表示で数十 MB に数分かかる）。

回帰ロック: `tests/test_installer.py::TestFieldFindings`（4 件）+ `-Update` の DryRun 通し。

4. **RDP ヘッドレス運用の設定**（インストーラの外。`docs/installation.md` §0「店舗 PC を画面なし・RDP で
   運用する場合」に手順化）: RDP のスマートカード転送を PC 側のポリシーで停止（`fEnableSmartCard=0`。
   既定では RDP セッション内のアプリに接続元端末のリーダーが見え、PC に挿した卓のリーダーが見えない）/
   店の Wi-Fi をプライベート + TCP 8788・8790 を LocalSubnet に限って許可（iPad / スマホから卓モニタ・API）。
5. **卓の USB が 2 本**（ESP32-S3-DevKitC-1 の「USB」= CCID 出力 / 「UART」= CP2102N の書き込み・ログ用）で、
   どちらがどちらか現地で分からなかった → Windows の PnP デバイスを VID/PID で見分ける 1 行で特定
   （`hardware-qa-checklist.md` §0 に表と手順）。USB 口が足りずハブを使う場合は、RFID とマイク受信機を
   直挿し・ハブはキーボードとマウスだけ、と案内。UART 側を外した場合、電源不足で起動しなかったリーダーは
   host には「カードなし」（SW=6A81）に見え `check` では見分けられないので、全台にカードを置く `watch` で確認する。
6. **結果**: インストール → 設定（RFID = PC/SC、マイク off）→ リーダー確認 → ハンドロガー（`--cli`）と
   卓モニタを起動し、**iPhone から卓モニタの表示と反映を確認**。店舗 PC での通しは完了。卓モニタの表示に
   気になる点があるとの報告あり（詳細は未受領）。
8. **`update.cmd` が既定の `verify-v1` を取りに行く問題**（ADR-0058 の反映前に判明）: 店舗 PC は別ブランチで
   入れているので、`update.cmd` をダブルクリックすると古い版で上書きしてしまう。取得元のブランチを
   `installer\branch.txt` に記録し、`-Branch` 無しの更新はそれを使うようにした（bootstrap も記録するので、
   店舗 PC の古い install.ps1 のままでも 1 行インストールで更新すれば以後の `update.cmd` が正しいブランチになる）。
   回帰: `TestFieldFindings::test_update_remembers_the_branch_it_was_installed_from` + DryRun 通し。
7. 依頼元の手順書との差分として伝えたこと: 7（Python）はインストーラに置き換え / 7.5 の rfid_cards.json を
   空にしない（登録済みの 52 枚）/ 9（ESP32 を Wi-Fi・HTTP 8787 で繋ぐ前提）は USB 接続なので不要 /
   10（自動起動）はヘッドレスでの操作の形を決めてから。

## Remaining Gaps / Out-of-Scope

- [x] **実 Windows での通し** — 2026-09-24 に店舗 PC で完了（上の「実機導入」）。
- [ ] Whisper モデル先読みの所要時間（今回はマイク未使用で skip）と、python.org フォールバック経路の実機確認
      （今回は手動の `winget --source winget` で通したため未通過）。
- [ ] 店舗 PC 向け設定（RDP のスマートカード転送停止 / ネットワーク種別 / ファイアウォール）を
      インストーラの任意手順にするか（管理者権限が要る・PC 全体に効くので既定では入れない前提で検討）。
- [ ] 画面なし運用での自動起動（依頼元手順書の 10）。`--cli` はキーボード入力前提なので、ハンドの開始と
      勝者を誰がどう入れるか（iPad の staff アプリ = GUI + session_layer + hand_control 等）を決めてから。
- [ ] 卓モニタの表示の気になる点（店舗 PC で報告、詳細待ち）。
- [ ] Stage 2: iPad / スマホ画面の API 配信（Node 不要化）/ データフォルダ分離 / release zip /
      PyInstaller + Inno Setup の exe / 自動起動。
- [ ] `update.cmd` は削除を行わない（上流で消えたファイルが残る）。
- [ ] `rfid_cards.json` は開発用デッキのまま同梱される。店舗ごとの登録手順は installation.md §0 に記載。

## 実 Windows での検証手順（オーナー用・コピペ）

`verify-v1` にはまだインストーラが入っていないので、**ブランチを環境変数で明示**する（マージ後は
`docs/installation.md` §0 の 1 行で足りる）:

```powershell
$env:POKERAPP_INSTALL_DIR="$HOME\PokerHandLogger-test"; $env:POKERAPP_BRANCH="claude/confident-hawking-5e4hff"; irm https://raw.githubusercontent.com/reo-sato/pokerapp/claude/confident-hawking-5e4hff/installer/bootstrap.ps1 | iex
```

## Related ADRs

- `docs/adr/0057-windows-one-step-installer.md`

## Related Issues

- ISSUE-0034（`--log-file`）

## Related Commits

- 本タスクのコミット（`feat(installer): Windows ワンステップインストーラ …`）。
