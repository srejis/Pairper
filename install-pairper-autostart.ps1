param([ValidateRange(0,300)][int]$DelaySeconds = 20, [switch]$ValidateOnly)
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$pairperRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$pairperStatusPath = Join-Path $pairperRoot '.pairper\status.json'
if (-not (Test-Path -LiteralPath $pairperStatusPath)) { throw 'First run and verify the tunnel manually.' }
$pairperPython = Join-Path $pairperRoot '.venv\Scripts\python.exe'
$pairperLiveText = & $pairperPython -m pairper.tunnel.runtime status --project-dir $pairperRoot
if ($LASTEXITCODE -ne 0) { throw 'Tunnel status check failed.' }
$pairperLive = $pairperLiveText | ConvertFrom-Json
if (-not $pairperLive.ready) { throw 'Local readiness must pass before installing autostart.' }
$pairperTaskName = 'Pairper MCP Tunnel'
$pairperUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$pairperScript = Join-Path $pairperRoot 'start-pairper-tunnel.ps1'
$pairperShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$pairperAction = New-ScheduledTaskAction -Execute $pairperShell -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$pairperScript`"" -WorkingDirectory $pairperRoot
$pairperTrigger = New-ScheduledTaskTrigger -AtLogOn -User $pairperUser
$pairperTrigger.Delay = "PT${DelaySeconds}S"
$pairperSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$pairperPrincipal = New-ScheduledTaskPrincipal -UserId $pairperUser -LogonType Interactive -RunLevel Limited
if ($ValidateOnly) {
    $pairperTask = New-ScheduledTask -Action $pairperAction -Trigger $pairperTrigger -Settings $pairperSettings -Principal $pairperPrincipal -Description "Pairper project: $pairperRoot"
    $pairperTask | Select-Object Description,@{Name='Action';Expression={$_.Actions.Execute}},@{Name='Delay';Expression={$_.Triggers.Delay}},@{Name='RunLevel';Expression={$_.Principal.RunLevel}},@{Name='ExecutionTimeLimit';Expression={$_.Settings.ExecutionTimeLimit}},@{Name='RestartCount';Expression={$_.Settings.RestartCount}},@{Name='MultipleInstances';Expression={$_.Settings.MultipleInstances}}
    Write-Output 'Validation only. No scheduled task was installed.'
    exit 0
}
$pairperExisting = Get-ScheduledTask -TaskName $pairperTaskName -ErrorAction SilentlyContinue
if ($pairperExisting -and ($pairperExisting.Description -ne "Pairper project: $pairperRoot")) {
    throw 'A task with this name belongs to a different project; it was not changed.'
}
Register-ScheduledTask -TaskName $pairperTaskName -Action $pairperAction -Trigger $pairperTrigger -Settings $pairperSettings -Principal $pairperPrincipal -Description "Pairper project: $pairperRoot" -Force | Select-Object TaskName,State
Write-Output 'Installed for the current user. No credentials were stored in the task.'
