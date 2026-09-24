# Poker Hand Logger — 1 行インストール（店舗 PC）。PowerShell に次を貼るだけ:
#
#   irm https://raw.githubusercontent.com/reo-sato/pokerapp/verify-v1/installer/bootstrap.ps1 | iex
#
# GitHub から zip を取得して C:\PokerHandLogger に展開し、installer\install.ps1 を実行する。
# 場所やブランチを変えるときは環境変数で: $env:POKERAPP_INSTALL_DIR / $env:POKERAPP_BRANCH / $env:POKERAPP_REPO
# 既に展開済みなら更新モード（設定・データは保持）。
#
# 書き方の制約（`irm … | iex` でユーザーの対話コンソールの中で実行されるため。tests/test_installer.py が固定）:
#   - param ブロックは使えない → 環境変数で受ける。
#   - `exit` を書かない → iex の中の exit はユーザーの PowerShell ウィンドウごと閉じ、結果が読めなくなる。
#   - ファイル先頭に UTF-8 BOM を付けない → Windows PowerShell 5.1 では irm が返す文字列の先頭に U+FEFF が
#     残り得て iex が失敗する（-File で実行する install.ps1 は逆に BOM 付き）。
#   - 全体を & { } で包み、変数や $ErrorActionPreference をユーザーのセッションに残さない。

& {
    $ErrorActionPreference = "Stop"
    # 5.1 の Invoke-WebRequest は進捗表示で極端に遅くなるので切る（& { } の中なのでセッションには残らない）。
    $ProgressPreference = "SilentlyContinue"
    # Windows PowerShell 5.1 は既定で TLS 1.2 を使わないことがある（GitHub は TLS 1.2 必須）。
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch { }

    $InstallDir = if ($env:POKERAPP_INSTALL_DIR) { $env:POKERAPP_INSTALL_DIR } else { "C:\PokerHandLogger" }
    $Branch     = if ($env:POKERAPP_BRANCH) { $env:POKERAPP_BRANCH } else { "verify-v1" }
    $Repo       = if ($env:POKERAPP_REPO) { $env:POKERAPP_REPO } else { "reo-sato/pokerapp" }
    $installer  = Join-Path $InstallDir "installer\install.ps1"

    if (Test-Path $installer) {
        Write-Host "既にインストール済みです ($InstallDir)。更新モードで実行します。" -ForegroundColor Cyan
        # .ps1 の直接実行は実行ポリシーに引っかかるので、明示的に Bypass で起動する。
        & powershell -NoProfile -ExecutionPolicy Bypass -File $installer -Update -Branch $Branch -Repo $Repo
    } else {
        $zipUrl  = "https://codeload.github.com/$Repo/zip/refs/heads/$Branch"
        $tmpRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("pokerapp-bootstrap-" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $tmpRoot | Out-Null
        try {
            Write-Host "取得中: $zipUrl" -ForegroundColor Cyan
            $zip = Join-Path $tmpRoot "src.zip"
            try {
                Invoke-WebRequest -Uri $zipUrl -OutFile $zip -UseBasicParsing
            } catch {
                # 店舗 PC で実測: 固定 IP にデフォルトゲートウェイが無いと、IPv6 だけ通ってこの 1 行目は取れるのに、
                # IPv4 しか持たない GitHub の zip 配布だけが「リモート名を解決できませんでした」で失敗する。
                throw ("GitHub から取得できませんでした: " + $_.Exception.Message +
                    " / 固定 IP にしている場合はデフォルトゲートウェイが入っているか確認してください（docs/installation.md §0 のトラブル表）。")
            }
            Expand-Archive -Path $zip -DestinationPath $tmpRoot -Force
            $src = Get-ChildItem -Path $tmpRoot -Directory | Select-Object -First 1   # pokerapp-<branch>
            if (-not $src) { throw "zip の展開結果が見つかりません" }
            if (-not (Test-Path (Join-Path $src.FullName "installer\install.ps1"))) {
                throw "取得した zip に installer\install.ps1 が含まれていません（ブランチ '$Branch' はインストーラ未対応です。`$env:POKERAPP_BRANCH を確認してください）"
            }
            New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
            Write-Host "展開先: $InstallDir" -ForegroundColor Cyan
            & robocopy $src.FullName $InstallDir /E /NFL /NDL /NJH /NJS /R:2 /W:2 | Out-Null
            if ($LASTEXITCODE -ge 8) { throw "robocopy に失敗しました（exit code $LASTEXITCODE）" }
        } finally {
            Remove-Item -Recurse -Force $tmpRoot -ErrorAction SilentlyContinue
        }
        & powershell -NoProfile -ExecutionPolicy Bypass -File $installer
    }

    if ($LASTEXITCODE -ne 0) {
        Write-Host "インストールに失敗しました (exit code $LASTEXITCODE)。$InstallDir\install.log を確認してください。" -ForegroundColor Red
    }
}
