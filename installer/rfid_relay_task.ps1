# RFID の中継（RDP のセッションの外でリーダーを読む）をスケジュールタスク PokerRFIDRelay として登録し、起動する。
# RDP で操作する店舗 PC では、RDP のセッションの中のアプリからリーダー（PC/SC）が見えない（店舗 2026-09-30）。
# 中継はシステムの権限で PC の起動時に動き、ロガーは動いている中継から札を受け取る（tools\rfid_relay.py / rfid\relay.py）。
# アプリを更新したあとも、もう一度実行すると中継が新しい版で動き直す。
#
# 管理者で実行する（PowerShell に貼る）:
#   Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File C:\PokerHandLogger\installer\rfid_relay_task.ps1'
# 消すときは末尾に -Remove を付ける。
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "venv\Scripts\python.exe"
$name = "PokerRFIDRelay"
try {
    if ($Remove) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "中継のタスク $name を消しました。"
    } else {
        if (-not (Test-Path $python)) { throw "venv が見つかりません（先に install.cmd を実行）: $python" }
        $action = New-ScheduledTaskAction -Execute $python -Argument "tools\rfid_relay.py serve" -WorkingDirectory $root
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
            -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
            -User "NT AUTHORITY\SYSTEM" -RunLevel Highest -Force | Out-Null
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
        Start-ScheduledTask -TaskName $name
        Write-Host "中継のタスク $name を登録して起動しました（PC の起動時にも自動で動きます）。"
        & $python (Join-Path $root "tools\rfid_relay.py") status --wait 20
    }
} catch {
    Write-Host "[error] $_" -ForegroundColor Red
}
Read-Host "Enter で閉じます"
