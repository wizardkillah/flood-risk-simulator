"""
Grilla de viento a 10m de altura sobre la Región de Coquimbo, para animar
partículas tipo Meteored/earth.nullschool en el visor 3D.

Fuente: Open-Meteo (mismo modelo que rainfall.py). Devuelve una serie
horaria de frames: cada frame tiene u (componente este, m/s) y v (norte,
m/s) por celda de la grilla — convertidos desde speed/direction
meteorológica ("direction FROM"), que es la convención que entrega la API.

Serie horaria (past 3h + forecast 1h = 4-5 frames) para poder sincronizar
las partículas con el slider de tiempo del overlay satelital: el frontend
elige el frame cuyo `time` está más cerca del timestamp seleccionado.

Cache: 30min. La cabecera cambia poco a esa escala y la request es de ~225
puntos × 4h, no queremos golpear la API a cada toggle.
"""

import math
import time
from datetime import datetime, timezone

import requests

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Grilla más densa que rainfall.py — necesaria para que la interpolación
# bilineal en el frontend no muestre discontinuidades feas al animar. La
# extendemos un poco fuera del bbox oficial para que las partículas cerca
# del borde (mar) tengan valores válidos y no salten.
GRID_NX = 15  # columnas (lon)
GRID_NY = 15  # filas (lat)
BBOX = {"north": -28.8, "south": -32.6, "east": -69.5, "west": -72.5}
CACHE_TTL_S = 30 * 60

_cache: dict = {"at": 0.0, "data": None}


def _build_grid() -> list[tuple[float, float]]:
    lats = [BBOX["south"] + (BBOX["north"] - BBOX["south"]) * i / (GRID_NY - 1)
            for i in range(GRID_NY)]
    lons = [BBOX["west"] + (BBOX["east"] - BBOX["west"]) * j / (GRID_NX - 1)
            for j in range(GRID_NX)]
    return [(lat, lon) for lat in lats for lon in lons]


def _speed_dir_to_uv(speed_ms: float, dir_deg_from: float) -> tuple[float, float]:
    """Convención meteorológica: dir es DE DONDE VIENE el viento (0 = del norte).
    u apunta al este, v al norte. Viento del norte (dir=0) sopla hacia el sur:
    u=0, v=-speed. Por eso u = -speed*sin(dir), v = -speed*cos(dir)."""
    if speed_ms is None or dir_deg_from is None:
        return 0.0, 0.0
    r = math.radians(dir_deg_from)
    return -speed_ms * math.sin(r), -speed_ms * math.cos(r)


def fetch_wind_grid(force: bool = False) -> dict:
    now = time.time()
    if not force and _cache["data"] is not None and (now - _cache["at"]) < CACHE_TTL_S:
        return _cache["data"]

    grid = _build_grid()
    lats = ",".join(f"{lat:.4f}" for lat, _ in grid)
    lons = ",".join(f"{lon:.4f}" for _, lon in grid)

    resp = requests.get(
        OPEN_METEO_URL,
        params={
            "latitude": lats,
            "longitude": lons,
            "hourly": "wind_speed_10m,wind_direction_10m",
            "wind_speed_unit": "ms",
            "past_hours": 3,       # cubre el rango del slider satelital (2h)
            "forecast_hours": 1,   # incluye la hora actual/próxima
            "timezone": "UTC",
        },
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json()

    # Todas las estaciones comparten el mismo eje temporal (Open-Meteo lo garantiza)
    times = results[0]["hourly"]["time"]
    n_hours = len(times)

    frames: list[dict] = []
    for h in range(n_hours):
        us: list[float] = []
        vs: list[float] = []
        speeds: list[float] = []
        for station in results:
            s = station["hourly"]["wind_speed_10m"][h]
            d = station["hourly"]["wind_direction_10m"][h]
            u, v = _speed_dir_to_uv(s, d)
            us.append(round(u, 3))
            vs.append(round(v, 3))
            speeds.append(round(s or 0.0, 2))
        frames.append({
            # Open-Meteo con timezone=UTC devuelve strings ISO sin sufijo Z.
            # Normalizamos con Z para que new Date(...) en el frontend lo
            # parsee como UTC y no como hora local.
            "time": times[h] if times[h].endswith("Z") else times[h] + "Z",
            # Row-major: idx = row(lat) * nx + col(lon). row 0 = south, col 0 = west.
            "u": us,
            "v": vs,
            "speed": speeds,
            "speed_max": max(speeds) if speeds else 0.0,
        })

    data = {
        "nx": GRID_NX,
        "ny": GRID_NY,
        "bbox": BBOX,
        "frames": frames,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "Open-Meteo hourly wind_10m (past 3h + forecast 1h) — modelo meteorológico, no medición directa",
    }
    _cache["at"] = now
    _cache["data"] = data
    return data
