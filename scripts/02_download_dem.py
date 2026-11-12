"""
Descarga el DEM (Copernicus GLO-30, 30m) de la cuenca del Limarí vía la API
de OpenTopography, usando el bbox ajustado por scripts/01_delimit_basin.py.

Requiere OPENTOPOGRAPHY_API_KEY en un archivo .env (ver .env.example).
"""

import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
HYDRO_DIR = ROOT / "data" / "hydro"
DEM_DIR = ROOT / "data" / "dem"

BBOX_PATH = HYDRO_DIR / "limari_bbox.json"
OUT_PATH = DEM_DIR / "limari_cop30.tif"

# Margen extra alrededor del bbox de la cuenca para no cortar el terreno
# justo en el borde del visor 3D. Valor arbitrario, ajustable.
PADDING_DEG = 0.05

API_URL = "https://portal.opentopography.org/API/globaldem"
DEM_TYPE = "COP30"  # Copernicus GLO-30


def download_dem():
    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("OPENTOPOGRAPHY_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "Falta OPENTOPOGRAPHY_API_KEY en .env. "
            "Crea una cuenta gratuita en https://portal.opentopography.org y "
            "genera una key en requestService?service=api"
        )

    if not BBOX_PATH.exists():
        raise SystemExit(
            f"No existe {BBOX_PATH}. Corre primero scripts/01_delimit_basin.py"
        )
    with open(BBOX_PATH, encoding="utf-8") as f:
        bbox_data = json.load(f)
    bbox = bbox_data["bbox_ajustado_hydrobasins"]

    params = {
        "demtype": DEM_TYPE,
        "south": bbox["south"] - PADDING_DEG,
        "north": bbox["north"] + PADDING_DEG,
        "west": bbox["west"] - PADDING_DEG,
        "east": bbox["east"] + PADDING_DEG,
        "outputFormat": "GTiff",
        "API_Key": api_key,
    }

    DEM_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Solicitando DEM {DEM_TYPE} a OpenTopography para bbox: {bbox}")
    resp = requests.get(API_URL, params=params, timeout=180)
    if resp.status_code != 200 or resp.headers.get("content-type", "").startswith("application/json"):
        raise SystemExit(f"Error de OpenTopography ({resp.status_code}): {resp.text[:500]}")

    with open(OUT_PATH, "wb") as f:
        f.write(resp.content)

    size_mb = OUT_PATH.stat().st_size / (1024 * 1024)
    print(f"Guardado: {OUT_PATH} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    download_dem()
