$ErrorActionPreference = 'Stop'
$pairperTaskName = 'Pairper MCP Tunnel'
$pairperExisting = Get-ScheduledTask -TaskName $pairperTaskName -ErrorAction SilentlyContinue
if (-not $pairperExisting) { Write-Output 'Pairper autostart is not installed.'; exit 0 }
$pairperRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
if ($pairperExisting.Description -ne "Pairper project: $pairperRoot") {
    throw 'The task belongs to a different project; it was not changed.'
}
Unregister-ScheduledTask -TaskName $pairperTaskName -Confirm:$false
Write-Output 'Pairper autostart removed. Other startup tasks were not changed.'
