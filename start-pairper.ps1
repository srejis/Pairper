param([switch]$SoftwareRendering, [switch]$LegacyUi, [string]$DevUi, [switch]$Tray)
$ErrorActionPreference = 'Stop'
if ($SoftwareRendering) {
    $env:QTWEBENGINE_CHROMIUM_FLAGS = '--disable-gpu'
    $env:QT_OPENGL = 'software'
}
$pairperPython = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pairperPython)) { throw 'Run uv sync --locked first.' }
$pairperArguments = @('-m','pairper.app.main')
if ($LegacyUi) { $pairperArguments += '--legacy-ui' }
if ($DevUi) { $pairperArguments += @('--dev-ui', $DevUi) }
if ($Tray) { $pairperArguments += '--tray' }
# pythonw has no console. Let Qt control the interactive window; Hidden can suppress its first show.
# --tray still prevents the Qt window from opening until the user restores it.
$pairperProcess = Start-Process -FilePath $pairperPython -ArgumentList $pairperArguments -WorkingDirectory $PSScriptRoot -WindowStyle Normal -Wait -PassThru
exit $pairperProcess.ExitCode
