param([string]$ClientPath)
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$pairperPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pairperPython)) { throw 'Run uv sync --locked first.' }
$pairperArguments = @('-m','pairper.tunnel.runtime','run','--project-dir',$PSScriptRoot)
if ($ClientPath) { $pairperArguments += @('--client-path',$ClientPath) }
# The child loader reads .env literally and passes secrets only to its own child process.
& $pairperPython @pairperArguments
exit $LASTEXITCODE
