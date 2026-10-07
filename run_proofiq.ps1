$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LocalOllamaExe = Join-Path $ProjectDir ".ollama-runtime\ollama.exe"
$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$OllamaUser = Join-Path $ProjectDir ".ollama-user"
$OllamaModels = Join-Path $OllamaUser ".ollama\models"

if (Test-Path $LocalOllamaExe) {
    $OllamaExe = $LocalOllamaExe
    $env:USERPROFILE = $OllamaUser
    $env:OLLAMA_MODELS = $OllamaModels
    New-Item -ItemType Directory -Force -Path $OllamaUser, $OllamaModels | Out-Null
} else {
    $OllamaCommand = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $OllamaCommand) {
        throw "Ollama is not installed. Download it from https://ollama.com/download, then run this script again."
    }
    $OllamaExe = $OllamaCommand.Source
}
if (-not (Test-Path $PythonExe)) {
    throw "Python environment is missing. Create .venv and install requirements.txt."
}

try {
    $OllamaTags = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 5
    $ModelReady = @($OllamaTags.models | Where-Object { $_.name -eq "qwen3.5:4b" }).Count -gt 0
    if (-not $ModelReady) { throw "Ollama is running without the ProofIQ model directory." }
} catch {
    Get-Process ollama -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $OllamaExe } |
        Stop-Process -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
    Start-Process -FilePath $OllamaExe -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 5
}

$OllamaTags = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 10
if (@($OllamaTags.models | Where-Object { $_.name -eq "qwen3.5:4b" }).Count -eq 0) {
    Write-Host "Downloading qwen3.5:4b once (about 3.3 GB)..." -ForegroundColor Yellow
    & $OllamaExe pull qwen3.5:4b
}

Write-Host "ProofIQ is starting at http://127.0.0.1:8000" -ForegroundColor Green
Write-Host "Local model: qwen3.5:4b (no API credits required)" -ForegroundColor Cyan
Set-Location $ProjectDir
& $PythonExe manage.py runserver 127.0.0.1:8000
