# 店舗サーバー（python main.py --serve, ADR-0065）をスケジュールタスク PokerStoreServer として登録し、起動する。
# 店舗サーバー = 会計の書き手の API（お客さん向け画面 / スタッフ用画面 /staff/ / 注文）+ 卓モニタ。画面を持たない。
# 店舗 PC は通電で起動し pokerpc で自動サインインする（店舗 PC の手順書 §3.3・§4）。そのサインインのときに動き、
# 止まっても 5 分以内に起動し直す。サインアウトすると止まる（RDP は右上の × で閉じる = 切断）。
# 録音（ハンドロガー）は含めない（席と名前を入れる対話式なので、RDP でつないで起動する）。
# 管理者の権限は要らない（自分がサインインしたときに自分の権限で動くタスク）。
#
# ダブルクリック: server_autostart.cmd。消すときは server_autostart.cmd remove
# PowerShell から: powershell -NoProfile -ExecutionPolicy Bypass -File C:\PokerHandLogger\installer\store_server_task.ps1 [-Remove]
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
# pythonw = 窓を出さない（サインインした画面に黒い窓が出ると、閉じたときに止まってしまう）。ログは logs\store_server.log
$python = Join-Path $root "venv\Scripts\pythonw.exe"
$name = "PokerStoreServer"

try {
    if ($Remove) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "店舗サーバーのタスク $name を消しました（サインインしても動きません）。"
    } else {
        if (-not (Test-Path $python)) { throw "venv が見つかりません（先に install.cmd を実行）: $python" }
        $user = "$env:USERDOMAIN\$env:USERNAME"
        $action = New-ScheduledTaskAction -Execute $python -Argument "main.py --serve" -WorkingDirectory $root
        # サインイン時 + 5 分ごと（動いていれば何もしない = MultipleInstances IgnoreNew。止まっていたら起動し直す）
        $atLogOn = New-ScheduledTaskTrigger -AtLogOn -User $user
        $every5min = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
            -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -MultipleInstances IgnoreNew -StartWhenAvailable
        # サインインしているこのユーザーの権限で動かす（Interactive = サインイン中だけ。パスワードは保存しない）
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $name -Action $action -Trigger @($atLogOn, $every5min) `
            -Settings $settings -Principal $principal -Force | Out-Null
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        Start-ScheduledTask -TaskName $name
        Write-Host "店舗サーバーのタスク $name を登録して起動しました（ユーザー $user がサインインしたときに自動で動きます）。"
        $ok = $false
        for ($i = 0; $i -lt 20 -and -not $ok; $i++) {
            Start-Sleep -Seconds 1
            try {
                $res = Invoke-WebRequest -Uri "http://127.0.0.1:8788/api/health" -UseBasicParsing -TimeoutSec 2
                $ok = ($res.StatusCode -eq 200)
            } catch { }
        }
        if ($ok) {
            Write-Host "動いています。iPad: http://<この PC の IPv4>:8788/staff/  お客さん: http://<この PC の IPv4>:8788/" -ForegroundColor Green
            ipconfig | Select-String "IPv4"
        } else {
            Write-Host "20 秒待っても応答がありません。$root\logs\store_server.log を確かめてください（会計画面や「お客さん用 ハンド履歴」が開いていれば閉じる）。" -ForegroundColor Yellow
        }
    }
} catch {
    Write-Host "[error] $_" -ForegroundColor Red
}
Read-Host "Enter で閉じます"
