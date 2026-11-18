# マイクの確認（ADR-0073 段階 0）: tools\console_probe.py を、タスク PokerConsoleProbe として RDP の外から動かす。
# iPad だけでロガーを起動するには、RDP の外（自動サインインしたコンソール）でロガーが録音できなければならない。
# このタスクは、いまから -Minutes 分（既定 60）と、次にサインインしたとき（PC の再起動のあと）にも同じ時間だけ動き、
# 5 秒ごとに「どの状態か（コンソール / RDP / 切断）・マイクを開けているか・声の大きさ」を logs\console_probe\ に書く。
# 管理者の権限は要らない。手順は docs\console-probe.md。
#
# ダブルクリック: console_probe.cmd（登録して起動）/ console_probe.cmd report（まとめを表示）/ console_probe.cmd remove
param([switch]$Remove, [switch]$Report, [int]$Minutes = 60)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root "venv\Scripts\pythonw.exe"
$python = Join-Path $root "venv\Scripts\python.exe"
$name = "PokerConsoleProbe"

try {
    if ($Report) {
        & $python (Join-Path $root "tools\console_probe.py") report
    } elseif ($Remove) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "マイクの確認のタスク $name を消しました。"
    } else {
        if (-not (Test-Path $pythonw)) { throw "venv が見つかりません（先に install.cmd を実行）: $pythonw" }
        $user = "$env:USERDOMAIN\$env:USERNAME"
        $seconds = $Minutes * 60
        $action = New-ScheduledTaskAction -Execute $pythonw -Argument "tools\console_probe.py run --seconds $seconds" -WorkingDirectory $root
        $atLogOn = New-ScheduledTaskTrigger -AtLogOn -User $user
        $now = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(30)
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes ($Minutes + 15)) `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
        # 店舗サーバーと同じ形（このユーザー・Interactive = サインインしているセッションの中で動く）
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $name -Action $action -Trigger @($now, $atLogOn) `
            -Settings $settings -Principal $principal -Force | Out-Null
        Start-ScheduledTask -TaskName $name
        Write-Host "マイクの確認を始めました（$Minutes 分。PC を再起動すると、サインインしたときにもう一度 $Minutes 分）。" -ForegroundColor Green
        Write-Host "手順: $root\docs\console-probe.md   まとめ: console_probe.cmd report   止める: schtasks /End /TN $name"
    }
} catch {
    Write-Host "[error] $_" -ForegroundColor Red
}
Read-Host "Enter で閉じます"
