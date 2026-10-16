$ErrorActionPreference='Stop'
. "$PSScriptRoot\pairper-autostart-common.ps1"
$task=Get-ScheduledTask -TaskPath '\' -ErrorAction Stop | Where-Object { $_.TaskName -eq 'Pairper' }
if ($task -and -not (Test-PairperTaskOwner $task (Join-Path $PSScriptRoot 'start-pairper.ps1'))) { throw 'Task ownership is unclear.' }
[PSCustomObject]@{enabled=[bool]($task -and $task.State -ne 'Disabled')} | ConvertTo-Json -Compress
