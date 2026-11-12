#!/usr/bin/env bash
# Launcher del Simulador de crecidas - Region de Coquimbo (macOS / Linux).
# Uso:  chmod +x iniciar.sh && ./iniciar.sh
# Ctrl+C detiene el servidor.

set -e
cd "$(dirname "$0")"

VENV_PY=".venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
    echo
    echo "[ERROR] No se encontro el entorno virtual .venv"
    echo "Crealo con:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

if [ ! -f ".env" ]; then
    echo "[AVISO] Falta .env. El asesor IA fallara con 503, el resto funciona."
    sleep 3
fi

if [ ! -f "data/flood/hand.tif" ]; then
    echo "[ERROR] Faltan los datos generados (data/flood/hand.tif)."
    echo "Corre los scripts 01..10 en orden (ver README.md)."
    exit 1
fi

# Puerto ocupado? (lsof preferido, ss como fallback en Linux)
if command -v lsof >/dev/null && lsof -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[AVISO] Puerto 8000 ocupado. Cierra la otra instancia antes de continuar."
    exit 1
fi

echo
echo "Iniciando simulador en http://localhost:8000 ..."
echo "(Ctrl+C detiene el servidor)"
echo

# Abrir navegador con delay segun plataforma
(
    sleep 3
    if command -v open >/dev/null; then open http://localhost:8000    # macOS
    elif command -v xdg-open >/dev/null; then xdg-open http://localhost:8000  # Linux
    fi
) &

exec "$VENV_PY" -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
