@echo off
setlocal
pushd "%~dp0"

if not exist ".venv\Scripts\python.exe" goto :sin_venv
if not exist "data\flood\hand.tif"      goto :sin_datos

if not exist ".env" (
    echo.
    echo [AVISO] Falta .env con las claves. El asesor IA fallara con HTTP 503,
    echo         pero el resto del visor funciona.
    echo.
    timeout /t 3 >nul
)

netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul
if not errorlevel 1 goto :puerto_ocupado

echo.
echo Iniciando simulador en http://localhost:8000 ...
echo (Ctrl+C detiene el servidor)
echo.
start "" cmd /c "timeout /t 3 >nul && start http://localhost:8000"

".venv\Scripts\python.exe" -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
goto :fin

:sin_venv
echo.
echo [ERROR] No se encontro el entorno virtual .venv
echo.
echo Crealo con:
echo     python -m venv .venv
echo     .venv\Scripts\pip install -r requirements.txt
echo.
pause
goto :fin

:sin_datos
echo.
echo [ERROR] Faltan los datos generados (data\flood\hand.tif).
echo         Corre los scripts 01..10 en orden (ver README.md).
echo.
pause
goto :fin

:puerto_ocupado
echo.
echo [AVISO] Puerto 8000 ocupado. Cierra la otra instancia antes de continuar.
echo.
pause
goto :fin

:fin
popd
endlocal
