$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
Write-Host "QQ GPT Companion starting (localhost only)..." -ForegroundColor Cyan
python -m companion.server --port 18761
