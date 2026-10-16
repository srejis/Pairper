param([ValidateRange(0,300)][int]$DelaySeconds=20, [switch]$ValidateOnly, [switch]$MigrateLegacyTunnelTask, [switch]$ShowWindow)
$ErrorActionPreference='Stop'
. "$PSScriptRoot\pairper-autostart-common.ps1"
$pairperRoot=[System.IO.Path]::GetFullPath($PSScriptRoot)
$pairperName='Pairper'
$pairperUser=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$pairperSid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$pairperDescription="Pairper project: $pairperRoot"
$pairperScript=Join-Path $pairperRoot 'start-pairper.ps1'
$pairperOldScript=Join-Path $pairperRoot 'start-pairper-tunnel.ps1'
$pairperShell=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath (Join-Path $pairperRoot '.venv\Scripts\pythonw.exe'))) { throw 'Run uv sync --locked first.' }
$pairperArguments="-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$pairperScript`""
if (-not $ShowWindow) { $pairperArguments+=' -Tray' }
$pairperAction=New-ScheduledTaskAction -Execute $pairperShell -Argument $pairperArguments -WorkingDirectory $pairperRoot
$pairperTrigger=New-ScheduledTaskTrigger -AtLogOn -User $pairperUser
$pairperTrigger.Delay="PT${DelaySeconds}S"
$pairperSettings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$pairperPrincipal=New-ScheduledTaskPrincipal -UserId $pairperUser -LogonType Interactive -RunLevel Limited
$pairperExisting=Get-ScheduledTask -TaskName $pairperName -ErrorAction SilentlyContinue
if ($pairperExisting -and -not (Test-PairperTaskOwner $pairperExisting $pairperScript)) { throw 'The existing Pairper task ownership is unclear. No task was changed.' }
$pairperOld=Get-ScheduledTask -TaskName 'Pairper MCP Tunnel' -ErrorAction SilentlyContinue
if ($pairperOld -and -not (Test-PairperTaskOwner $pairperOld $pairperOldScript)) { throw 'Legacy task ownership is unclear. No task was changed.' }
[PSCustomObject]@{TaskName=$pairperName;Command=$pairperShell;Arguments=$pairperArguments;WorkingDirectory=$pairperRoot;DelaySeconds=$DelaySeconds;RunLevel='Limited';ExecutionTimeLimit='PT0S';RestartCount=3;MultipleInstances='IgnoreNew';LegacyTaskPresent=[bool]$pairperOld;LegacyMigrationApproved=[bool]$MigrateLegacyTunnelTask}
if ($ValidateOnly) { Write-Output 'Validation only. No task was registered, disabled or stopped.'; exit 0 }
if ($pairperOld -and $pairperOld.State -ne 'Disabled' -and -not $MigrateLegacyTunnelTask) { throw 'Run again with -MigrateLegacyTunnelTask to explicitly approve disabling/stopping the owned legacy tunnel task.' }
Register-ScheduledTask -TaskName $pairperName -Action $pairperAction -Trigger $pairperTrigger -Settings $pairperSettings -Principal $pairperPrincipal -Description $pairperDescription -Force | Select-Object TaskName,State
if ($pairperOld -and $MigrateLegacyTunnelTask) {
    Disable-ScheduledTask -TaskName 'Pairper MCP Tunnel' | Out-Null
    if ($pairperOld.State -eq 'Running') { Stop-ScheduledTask -TaskName 'Pairper MCP Tunnel' }
    Write-Output 'The verified owned legacy task was disabled; its running instance was stopped.'
}
Write-Output 'Pairper desktop autostart installed. No credentials are stored in the task.'
