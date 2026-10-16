$ErrorActionPreference='Stop'
$pairperRoot=[System.IO.Path]::GetFullPath($PSScriptRoot)
$pairperTask=Get-ScheduledTask -TaskName 'Pairper' -ErrorAction SilentlyContinue
if (-not $pairperTask) { Write-Output 'Pairper desktop autostart is not installed.'; exit 0 }
$pairperUser=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$pairperSid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if ($pairperTask.Description -ne "Pairper project: $pairperRoot" -or $pairperTask.Actions.Count -ne 1 -or -not $pairperTask.Actions[0].Arguments.Contains((Join-Path $pairperRoot 'start-pairper.ps1')) -or $pairperTask.Principal.UserId -notin @($pairperUser,$pairperSid)) { throw 'Task ownership is unclear. No task was changed.' }
Unregister-ScheduledTask -TaskName 'Pairper' -Confirm:$false
Write-Output 'Pairper desktop autostart removed. The running app and other tasks were not changed.'
