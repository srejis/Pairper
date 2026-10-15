param([string]$ClientPath, [switch]$StatusOnly)
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$pairperPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pairperPython)) { throw 'Run uv sync --locked first.' }
$pairperAction = if ($StatusOnly) { 'status' } else { 'doctor' }
$pairperArguments = @('-m','pairper.tunnel.runtime',$pairperAction,'--project-dir',$PSScriptRoot)
if ($ClientPath) { $pairperArguments += @('--client-path',$ClientPath) }
& $pairperPython @pairperArguments
exit $LASTEXITCODE
