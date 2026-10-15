param([switch]$SoftwareRendering, [switch]$LegacyUi, [string]$DevUi)
$ErrorActionPreference = 'Stop'
if ($SoftwareRendering) {
    $env:QTWEBENGINE_CHROMIUM_FLAGS = '--disable-gpu'
    $env:QT_OPENGL = 'software'
}
$pairperExecutable = Join-Path $PSScriptRoot '.venv\Scripts\pairper.exe'
if (-not (Test-Path -LiteralPath $pairperExecutable)) {
    throw 'Run uv sync --locked in the project folder first.'
}
$pairperUiArguments = @()
if ($LegacyUi) { $pairperUiArguments += '--legacy-ui' }
if ($DevUi) { $pairperUiArguments += @('--dev-ui',$DevUi) }
& $pairperExecutable @pairperUiArguments
