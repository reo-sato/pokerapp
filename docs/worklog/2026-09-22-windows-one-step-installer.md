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
- `tests/test_installer.py` — **新規**（28 件）。
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
3. **エンコーディング**: `.ps1` は BOM 付き UTF-8（5.1 対策）、`.cmd` は ASCII のみ（CP932 対策）。
   日本語の案内は .ps1 側。
4. **stderr の扱い**: 5.1 では `2>&1` + `$ErrorActionPreference=Stop` でネイティブコマンドの stderr が
   例外になる（pip の progress で落ちる）ので、`Invoke-Checked` は stderr を端末に流し exit code だけ見る。
5. **保持リストの整合をテストで固定**: `$PreservedFiles` ⊇ `core/backup.py` の `_ROOT / "*.json"`。
6. **DryRun** は Linux の pwsh でも全手順を通る（Windows 固有 API は DryRun で触らない）。

## Test Results

- `pytest tests/test_installer.py`（pwsh あり）— **28 passed**（構文解析 2 + DryRun 通し 1 を含む）。
- `pytest tests/ -q --ignore=tests/test_vision.py` — pwsh あり **1176 passed** / pwsh 無し **1173 passed,
  3 skipped**（skipped = PowerShell 検査 3 件。CI の ubuntu ランナーには pwsh があるので実行される）。
- `ruff check .` — clean。
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

## Fixes Applied

上記 2 件。

## Remaining Gaps / Out-of-Scope

- [ ] **実 Windows での通し**（オーナーの PC。手順は本 worklog 末尾）。特に winget の無い環境での
      python.org フォールバックと、Whisper モデル先読みの所要時間。
- [ ] Stage 2: iPad / スマホ画面の API 配信（Node 不要化）/ データフォルダ分離 / release zip /
      PyInstaller + Inno Setup の exe / 自動起動。
- [ ] `update.cmd` は削除を行わない（上流で消えたファイルが残る）。
- [ ] `rfid_cards.json` は開発用デッキのまま同梱される。店舗ごとの登録手順は installation.md §0 に記載。

## 実 Windows での検証手順（オーナー用・コピペ）

```powershell
$env:POKERAPP_INSTALL_DIR="$HOME\PokerHandLogger-test"; irm https://raw.githubusercontent.com/reo-sato/pokerapp/claude/confident-hawking-5e4hff/installer/bootstrap.ps1 | iex
```

## Related ADRs

- `docs/adr/0057-windows-one-step-installer.md`

## Related Issues

- ISSUE-0034（`--log-file`）

## Related Commits

- 本タスクのコミット（`feat(installer): Windows ワンステップインストーラ …`）。
