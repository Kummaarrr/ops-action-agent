# Launch scripts/run_chain.sh detached from the current session (survives the editor closing).
#   powershell -ExecutionPolicy Bypass -File scripts/start_chain.ps1 -Stage test   (default dev)
param([string]$Stage = "")
if (-not $Stage) { $Stage = if ($env:CHAIN_STAGE) { $env:CHAIN_STAGE } else { "dev" } }
$root = Split-Path -Parent $PSScriptRoot
$bash = "C:\Program Files\Git\bin\bash.exe"
if (-not (Test-Path $bash)) { $bash = (Get-Command bash).Source }
$script = (Join-Path $root "scripts\run_chain.sh") -replace "\\", "/"
Start-Process -FilePath $bash -ArgumentList "-lc `"'$script' $Stage`"" -WorkingDirectory $root -WindowStyle Hidden
Write-Output "chain started (stage $Stage); follow $root\evals\results\chain.log"
