$ErrorActionPreference='Stop'
. "$PSScriptRoot\pairper-autostart-common.ps1"
$pairperRoot=[System.IO.Path]::GetFullPath($PSScriptRoot)
$pairperTask=Get-ScheduledTask -TaskName 'Pairper' -ErrorAction SilentlyContinue
if (-not $pairperTask) { Write-Output 'Pairper desktop autostart is not installed.'; exit 0 }
$pairperUser=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$pairperSid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if (-not (Test-PairperTaskOwner $pairperTask (Join-Path $pairperRoot 'start-pairper.ps1'))) { throw 'Task ownership is unclear. No task was changed.' }
Unregister-ScheduledTask -TaskName 'Pairper' -Confirm:$false
Write-Output 'Pairper desktop autostart removed. The running app and other tasks were not changed.'
