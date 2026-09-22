#Requires -Version 5.1
<#
.SYNOPSIS
    Poker Hand Logger — Windows ワンステップインストーラ（店舗 PC 向け）

.DESCRIPTION
    このスクリプトがあるフォルダの親（= アプリのフォルダ）に対して次を行う。
      1. Python 3.12 を確認（無ければ winget → python.org のサイレントインストールで導入）
      2. venv を作成し、依存パッケージを導入（pip install -e ".[pcsc,api]"）
      3. config.json を雛形（config_default.json）から生成（既存は上書きしない）
      4. 音声認識モデルを先読み（任意。あとでも可）
      5. デスクトップにショートカットを作成
      6. 動作確認（import と main.py --help）

    通常は install.cmd をダブルクリックすれば済む（PowerShell の実行ポリシーは .cmd 側で Bypass する）。

    -Update      GitHub の zip（-Branch）を上書き展開して更新する。設定・データ・rfid_cards.json は保持。
                 .git があり git が使えるなら git pull --ff-only。
    -Uninstall   ショートカットと venv を削除する（データは残す）。
    -PrefetchModel / -SkipModel   音声認識モデルの先読みを 質問せずに 行う / 行わない。
    -NonInteractive               質問しない（CI / 無人インストール用）。
    -DryRun      何をするかを表示するだけで何も変更しない。

    ログ: <アプリフォルダ>\install.log
#>
[CmdletBinding()]
param(
    [switch]$Update,
    [switch]$Uninstall,
    [switch]$PrefetchModel,
    [switch]$SkipModel,
    [switch]$NoShortcuts,
    [switch]$NonInteractive,
    [switch]$DryRun,
    [string]$Branch = "verify-v1",
    [string]$Repo = "reo-sato/pokerapp",
    [string]$PythonInstallerUrl = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
)

$ErrorActionPreference = "Stop"
# Windows PowerShell 5.1 は既定で TLS 1.2 を使わないことがある（python.org / GitHub は TLS 1.2 必須）。
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

# 3.12 に固定する理由: PyAudio / pyscard / ctranslate2（faster-whisper）の Windows wheel が揃っている版。
$PythonMajorMinor = "3.12"
$AppDir     = Split-Path -Parent $PSScriptRoot
$VenvDir    = Join-Path $AppDir "venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$LogPath    = Join-Path $AppDir "install.log"

# 更新時に保持するもの（店舗固有の設定・データ）。core/backup.py のデータ一覧と揃える
# （tests/test_installer.py が両者の整合を検査する）。
$PreservedFiles = @(
    "config.json", "rfid_cards.json", "menu.json",
    "players.json", "sessions.json", "ledger.json", "order_requests.json",
    "player_credentials.json", "auth_identity.json", "hand_corrections.json",
    "install.log"
)
$PreservedDirs = @("venv", "logs", "backups")

# デスクトップに作るショートカット（表示名 → 起動する .cmd）
$Shortcuts = @(
    @{ Name = "ハンドロガー (CLI)";        Target = "start_logger.cmd" },
    @{ Name = "卓モニタ (iPad から閲覧)";   Target = "start_monitor.cmd" },
    @{ Name = "会計 + スマホ注文 API";      Target = "start_ledger.cmd" },
    @{ Name = "RFID リーダー チェック";     Target = "rfid_check.cmd" }
)

# ───────────────────────── ユーティリティ ─────────────────────────

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    if (-not $DryRun) {
        try { Add-Content -Path $LogPath -Value $line -Encoding UTF8 } catch { }
    }
    switch ($Level) {
        "WARN"  { Write-Host $Message -ForegroundColor Yellow }
        "ERROR" { Write-Host $Message -ForegroundColor Red }
        "STEP"  { Write-Host ""; Write-Host ("== " + $Message) -ForegroundColor Cyan }
        default { Write-Host $Message }
    }
}

function Invoke-Checked {
    # ネイティブコマンドを実行し、exit code が 0 でなければ throw する。
    # stderr は端末にそのまま出す（2>&1 で success stream に混ぜると 5.1 では NativeCommandError になるため）。
    param([string]$Exe, [string[]]$Arguments, [string]$What)
    Write-Log ("$ " + $Exe + " " + ($Arguments -join " "))
    if ($DryRun) { return }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Exe @Arguments
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($code -ne 0) { throw "$What に失敗しました（exit code $code）" }
}

function Get-TempDir {
    return [System.IO.Path]::GetTempPath()
}

function Get-DesktopDir {
    $d = [Environment]::GetFolderPath("Desktop")
    if (-not $d) { $d = "<Desktop>" }
    return $d
}

function Update-ProcessPath {
    # インストーラが書いた PATH は現在のプロセスに反映されないので、レジストリから読み直す。
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user    = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = ($machine, $user, $env:Path) -join ";"
}

# ───────────────────────── Python ─────────────────────────

function Get-PythonCandidates {
    $c = @()
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { $c += @{ Exe = $py.Source; Prefix = @("-$PythonMajorMinor") } }
    $known = @()
    if ($env:LOCALAPPDATA) { $known += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe") }
    if ($env:ProgramFiles) { $known += (Join-Path $env:ProgramFiles "Python312\python.exe") }
    foreach ($p in $known) { if (Test-Path $p) { $c += @{ Exe = $p; Prefix = @() } } }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { $c += @{ Exe = $python.Source; Prefix = @() } }
    return $c
}

function Find-Python {
    # 3.12 の python.exe の絶対パスを返す。見つからなければ $null。
    # ESP-IDF 等の別バージョンや Microsoft Store のスタブ（exit code 9009）はここで弾かれる。
    $wanted = "(" + $PythonMajorMinor.Replace(".", ", ") + ")"
    $probe = "import sys; print(sys.executable) if sys.version_info[:2] == $wanted else sys.exit(1)"
    foreach ($cand in Get-PythonCandidates) {
        try {
            $prev = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            $arguments = @($cand.Prefix) + @("-c", $probe)
            $out = & $cand.Exe @arguments 2>$null
            $code = $LASTEXITCODE
            $ErrorActionPreference = $prev
            if ($code -eq 0 -and $out) {
                return ([string](@($out) | Select-Object -Last 1)).Trim()
            }
        } catch {
            $ErrorActionPreference = "Stop"
        }
    }
    return $null
}

function Install-Python {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Log "winget で Python $PythonMajorMinor を導入します（数分かかります）..."
        if (-not $DryRun) {
            $prev = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            & $winget.Source install --id "Python.Python.$PythonMajorMinor" --exact --silent `
                --accept-package-agreements --accept-source-agreements
            $ErrorActionPreference = $prev
            # winget の exit code は「既に導入済み」等でも 0 以外になるので、成否は Find-Python で判定する。
        }
    } else {
        $tmp = Join-Path (Get-TempDir) ("python-" + $PythonMajorMinor + "-installer.exe")
        Write-Log "winget が無いので python.org からインストーラを取得します: $PythonInstallerUrl"
        if (-not $DryRun) {
            Invoke-WebRequest -Uri $PythonInstallerUrl -OutFile $tmp -UseBasicParsing
            $p = Start-Process -FilePath $tmp -Wait -PassThru -ArgumentList @(
                "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_launcher=1",
                "Include_test=0", "Include_doc=0"
            )
            if ($p.ExitCode -ne 0) { throw "Python のインストールに失敗しました（exit code $($p.ExitCode)）" }
        }
    }
    Update-ProcessPath
}

# ───────────────────────── venv / 依存 / 設定 ─────────────────────────

function Initialize-Venv {
    param([string]$SystemPython)
    if (Test-Path $VenvPython) { Write-Log "venv は既にあります: $VenvDir"; return }
    Invoke-Checked $SystemPython @("-m", "venv", $VenvDir) "venv の作成"
}

function Install-Dependencies {
    Push-Location $AppDir
    try {
        Invoke-Checked $VenvPython @("-m", "pip", "install", "--upgrade", "pip") "pip の更新"
        # -e（editable）: データファイルや tools/ はアプリのフォルダ直下にある前提なので、
        # site-packages にコピーせずこのフォルダを指したまま入れる。
        Invoke-Checked $VenvPython @("-m", "pip", "install", "-e", ".[pcsc,api]") "依存パッケージの導入"
    } finally { Pop-Location }
}

function Initialize-Config {
    $cfg = Join-Path $AppDir "config.json"
    if (Test-Path $cfg) { Write-Log "config.json は既にあります（上書きしません）"; return }
    Write-Log "config.json を config_default.json から作成します"
    if (-not $DryRun) { Copy-Item (Join-Path $AppDir "config_default.json") $cfg }
}

function Get-ConfiguredModel {
    $cfg = Join-Path $AppDir "config.json"
    if (-not (Test-Path $cfg)) { return "medium" }
    try {
        $j = Get-Content $cfg -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($j.audio -and $j.audio.whisper_model) { return [string]$j.audio.whisper_model }
    } catch { }
    return "medium"
}

function Invoke-ModelPrefetch {
    if ($SkipModel) { Write-Log "モデルの先読みはスキップ（初回起動時に自動ダウンロードされます）"; return }
    $model = Get-ConfiguredModel
    $do = [bool]$PrefetchModel
    if (-not $do -and -not $NonInteractive) {
        $ans = Read-Host "音声認識モデル '$model'（medium は約 1.5 GB）を今ダウンロードしますか？ ネット接続が必要です [Y/n]"
        $do = ($ans -eq "" -or $ans -match "^[Yy]")
    }
    if (-not $do) { Write-Log "モデルの先読みはスキップ（初回起動時に自動ダウンロードされます）"; return }
    $code = "from faster_whisper import WhisperModel; WhisperModel('$model', device='cpu', compute_type='int8'); print('model ready: $model')"
    Invoke-Checked $VenvPython @("-c", $code) "音声認識モデルの取得"
}

# ───────────────────────── ショートカット / 動作確認 ─────────────────────────

function New-Shortcuts {
    if ($NoShortcuts) { return }
    $desktop = Get-DesktopDir
    $ws = $null
    if (-not $DryRun) { $ws = New-Object -ComObject WScript.Shell }
    foreach ($s in $Shortcuts) {
        $lnk = Join-Path $desktop ($s.Name + ".lnk")
        $target = Join-Path $AppDir $s.Target
        Write-Log ("ショートカット: " + $lnk + " -> " + $target)
        if ($DryRun) { continue }
        $sc = $ws.CreateShortcut($lnk)
        $sc.TargetPath = $target
        $sc.WorkingDirectory = $AppDir
        $sc.Description = "Poker Hand Logger"
        $sc.Save()
    }
}

function Remove-Shortcuts {
    $desktop = Get-DesktopDir
    foreach ($s in $Shortcuts) {
        $lnk = Join-Path $desktop ($s.Name + ".lnk")
        if (Test-Path $lnk) {
            Write-Log "削除: $lnk"
            if (-not $DryRun) { Remove-Item $lnk -Force }
        }
    }
}

function Test-Installation {
    $code = "import pokerkit, customtkinter, faster_whisper, pyaudio, smartcard, fastapi, uvicorn; print('imports ok')"
    Invoke-Checked $VenvPython @("-c", $code) "動作確認（import）"
    Push-Location $AppDir
    try { Invoke-Checked $VenvPython @("main.py", "--help") "動作確認（main.py --help）" }
    finally { Pop-Location }
}

# ───────────────────────── 更新 / アンインストール ─────────────────────────

function Invoke-Update {
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git -and (Test-Path (Join-Path $AppDir ".git"))) {
        Write-Log "git で更新します（git pull --ff-only）"
        Push-Location $AppDir
        try { Invoke-Checked $git.Source @("pull", "--ff-only") "git pull" } finally { Pop-Location }
        return
    }
    $zipUrl = "https://codeload.github.com/$Repo/zip/refs/heads/$Branch"
    Write-Log "GitHub から取得: $zipUrl"
    Write-Log ("保持するもの: " + (($PreservedFiles + $PreservedDirs) -join ", "))
    if ($DryRun) { return }
    $tmpRoot = Join-Path (Get-TempDir) ("pokerapp-update-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmpRoot | Out-Null
    try {
        $zip = Join-Path $tmpRoot "src.zip"
        Invoke-WebRequest -Uri $zipUrl -OutFile $zip -UseBasicParsing
        Expand-Archive -Path $zip -DestinationPath $tmpRoot -Force
        $src = Get-ChildItem -Path $tmpRoot -Directory | Select-Object -First 1   # pokerapp-<branch>
        if (-not $src) { throw "zip の展開結果が見つかりません" }
        # robocopy: 追加・上書きのみ（削除はしない）。/XD /XF で店舗固有のものを除外。
        $arguments = @($src.FullName, $AppDir, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/R:2", "/W:2", "/XD") `
            + $PreservedDirs + @("/XF") + $PreservedFiles
        Write-Log ("$ robocopy " + ($arguments -join " "))
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & robocopy @arguments | Out-Null
        $code = $LASTEXITCODE
        $ErrorActionPreference = $prev
        if ($code -ge 8) { throw "robocopy に失敗しました（exit code $code）" }
        Write-Log "ファイルを更新しました（設定・データは保持）"
    } finally {
        Remove-Item -Recurse -Force $tmpRoot -ErrorAction SilentlyContinue
    }
}

function Invoke-Uninstall {
    Remove-Shortcuts
    if (Test-Path $VenvDir) {
        Write-Log "削除: $VenvDir"
        if (-not $DryRun) { Remove-Item -Recurse -Force $VenvDir }
    }
    Write-Log "データ（logs\ / *.json / config.json）は残しています: $AppDir"
    Write-Log "完全に消すにはこのフォルダごと削除してください。"
}

# ───────────────────────── main ─────────────────────────

$mode = "install"
if ($Uninstall) { $mode = "uninstall" } elseif ($Update) { $mode = "update" }
Write-Log "Poker Hand Logger インストーラ (mode: $mode, app: $AppDir)" "STEP"
if ($DryRun) { Write-Log "DryRun: 表示のみで何も変更しません" "WARN" }

try {
    if ($Uninstall) { Invoke-Uninstall; exit 0 }
    if ($Update) { Write-Log "ファイルの更新" "STEP"; Invoke-Update }

    Write-Log "Python $PythonMajorMinor を確認" "STEP"
    $py = Find-Python
    if (-not $py) {
        Write-Log "Python $PythonMajorMinor が見つかりません。導入します。" "WARN"
        Install-Python
        $py = Find-Python
        if (-not $py -and $DryRun) { $py = "<python.exe>" }
    }
    if (-not $py) {
        throw "Python $PythonMajorMinor を用意できませんでした。https://www.python.org/downloads/ から 3.12 を入れて（「Add python.exe to PATH」にチェック）再実行してください。"
    }
    Write-Log "Python: $py"

    Write-Log "venv と依存パッケージ" "STEP"
    Initialize-Venv $py
    Install-Dependencies

    Write-Log "設定ファイル" "STEP"
    Initialize-Config

    Write-Log "音声認識モデル" "STEP"
    Invoke-ModelPrefetch

    Write-Log "ショートカット" "STEP"
    New-Shortcuts

    Write-Log "動作確認" "STEP"
    Test-Installation

    Write-Log "完了" "STEP"
    Write-Log "デスクトップのショートカットから起動できます。"
    Write-Log "RFID を使う場合は config.json の rfid.enabled を true にし、「RFID リーダー チェック」で接続を確認してください。"
    Write-Log "iPad / スマホから見る場合は config.json の viewer_api.bind_host を 0.0.0.0 に（信頼できる店内 Wi-Fi のみ）。"
    exit 0
} catch {
    Write-Log ("失敗: " + $_.Exception.Message) "ERROR"
    Write-Log "詳細は $LogPath を確認してください。" "ERROR"
    exit 1
}
