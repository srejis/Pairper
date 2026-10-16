function Test-PairperTaskOwner($Task, [string]$Script) {
    if ($Task.Description -ne "Pairper project: $PSScriptRoot" -or $Task.Actions.Count -ne 1 -or -not $Task.Actions[0].Arguments -or -not $Task.Actions[0].Arguments.Contains($Script)) { return $false }
    try {
        $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
        $owner = $Task.Principal.UserId
        if ($owner -eq $identity.User.Value) { return $true }
        $account = New-Object System.Security.Principal.NTAccount($owner)
        return $account.Translate([System.Security.Principal.SecurityIdentifier]).Value -eq $identity.User.Value
    } catch { return $false }
}
