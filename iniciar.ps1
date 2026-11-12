# Launcher del Simulador de crecidas - Region de Coquimbo (PowerShell).
# Uso: click derecho -> Ejecutar con PowerShell, o desde una terminal:
#   pwsh -ExecutionPolicy Bypass -File .\iniciar.ps1
# Ctrl+C detiene el servidor.

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Test-Path '.venv\Scripts\python.exe')) {
    Write-Host ""
    Write-Host "[ERROR] No se encontro el entorno virtual .venv" -ForegroundColor Red
    Write-Host "Crealo con:  python -m venv .venv;  .venv\Scripts\pip install -r requirements.txt"
    Read-Host "Presiona Enter para salir"
    exit 1
}

if (-not (Test-Path '.env')) {
    Write-Host "[AVISO] Falta .env. El asesor IA fallara con 503, el resto funciona." -ForegroundColor Yellow
    Start-Sleep -Seconds 3
}

if (-not (Test-Path 'data\flood\hand.tif')) {
    Write-Host "[ERROR] Faltan los datos generados (data\flood\hand.tif)." -ForegroundColor Red
    Write-Host "Corre los scripts 01..10 en orden (ver README.md)."
    Read-Host "Presiona Enter para salir"
    exit 1
}

$ocupado = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($ocupado) {
    Write-Host "[AVISO] Puerto 8000 ocupado. Cierra la otra instancia antes de continuar." -ForegroundColor Yellow
    Read-Host "Presiona Enter para salir"
    exit 1
}

Write-Host ""
Write-Host "Iniciando simulador en http://localhost:8000 ..." -ForegroundColor Cyan
Write-Host "(Ctrl+C detiene el servidor)"
Write-Host ""

# Abrir navegador con delay, y uvicorn en primer plano
Start-Job -ScriptBlock { Start-Sleep -Seconds 3; Start-Process 'http://localhost:8000' } | Out-Null

& '.venv\Scripts\python.exe' -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
