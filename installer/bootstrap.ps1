#Requires -Version 5.1
# Poker Hand Logger — 1 行インストール（店舗 PC）。PowerShell に次を貼るだけ:
#
#   irm https://raw.githubusercontent.com/reo-sato/pokerapp/verify-v1/installer/bootstrap.ps1 | iex
#
# GitHub から zip を取得して C:\PokerHandLogger に展開し、installer\install.ps1 を実行する。
# 場所やブランチを変えるときは環境変数で: $env:POKERAPP_INSTALL_DIR / $env:POKERAPP_BRANCH
# （`| iex` で流し込む都合上 param ブロックは使えない）。既に展開済みなら更新モード（設定・データは保持）。

$ErrorActionPreference = "Stop"
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

$InstallDir = if ($env:POKERAPP_INSTALL_DIR) { $env:POKERAPP_INSTALL_DIR } else { "C:\PokerHandLogger" }
$Branch     = if ($env:POKERAPP_BRANCH) { $env:POKERAPP_BRANCH } else { "verify-v1" }
$Repo       = if ($env:POKERAPP_REPO) { $env:POKERAPP_REPO } else { "reo-sato/pokerapp" }

$installer = Join-Path $InstallDir "installer\install.ps1"
if (Test-Path $installer) {
    Write-Host "既にインストール済みです ($InstallDir)。更新モードで実行します。" -ForegroundColor Cyan
    & powershell -NoProfile -ExecutionPolicy Bypass -File $installer -Update -Branch $Branch -Repo $Repo
    exit $LASTEXITCODE
}

$zipUrl  = "https://codeload.github.com/$Repo/zip/refs/heads/$Branch"
$tmpRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("pokerapp-bootstrap-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmpRoot | Out-Null
try {
    Write-Host "取得中: $zipUrl" -ForegroundColor Cyan
    $zip = Join-Path $tmpRoot "src.zip"
    Invoke-WebRequest -Uri $zipUrl -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath $tmpRoot -Force
    $src = Get-ChildItem -Path $tmpRoot -Directory | Select-Object -First 1   # pokerapp-<branch>
    if (-not $src) { throw "zip の展開結果が見つかりません" }
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    Write-Host "展開先: $InstallDir" -ForegroundColor Cyan
    & robocopy $src.FullName $InstallDir /E /NFL /NDL /NJH /NJS /R:2 /W:2 | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy に失敗しました（exit code $LASTEXITCODE）" }
} finally {
    Remove-Item -Recurse -Force $tmpRoot -ErrorAction SilentlyContinue
}

# .ps1 ファイルの直接実行は実行ポリシーに引っかかるので、明示的に Bypass で起動する。
& powershell -NoProfile -ExecutionPolicy Bypass -File $installer
exit $LASTEXITCODE
