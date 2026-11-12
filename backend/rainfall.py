"""
Obtiene precipitación acumulada de las últimas 24h sobre la cuenca del
Limarí vía Open-Meteo (sin API key, sin cuenta).

SUPUESTO NO VERIFICADO: Open-Meteo entrega datos de modelo meteorológico
(ICON/ECMWF/GFS, "best match"), no medición satelital ni de estaciones en
tierra. Resolución ~9-25km — mucho más gruesa que la cuenca real. Es una
aproximación razonable para un prototipo, no un reemplazo de estaciones
pluviométricas oficiales de la DGA.
"""

import time
from datetime import datetime, timedelta, timezone

import requests

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Grilla de puntos de muestreo sobre el bbox de la Región de Coquimbo completa
# (ver data/hydro/limari_bbox.json). 8x8 = 64 puntos, suficiente para una
# capa visual gruesa dado que Open-Meteo ya es ~9-25km de resolución.
GRID_N = 8
BBOX = {"north": -29.0, "south": -32.35, "east": -69.75, "west": -71.80}

_cache = {"timestamp": 0, "data": None}
CACHE_TTL_SECONDS = 15 * 60


def _build_grid():
    lats = [
        BBOX["south"] + (BBOX["north"] - BBOX["south"]) * i / (GRID_N - 1)
        for i in range(GRID_N)
    ]
    lons = [
        BBOX["west"] + (BBOX["east"] - BBOX["west"]) * j / (GRID_N - 1)
        for j in range(GRID_N)
    ]
    return [(lat, lon) for lat in lats for lon in lons]


def fetch_rainfall_24h(force: bool = False) -> dict:
    """Devuelve {points: [{lat, lon, mm_24h}], basin_average_mm, fetched_at}."""
    now = time.time()
    if not force and _cache["data"] is not None and (now - _cache["timestamp"]) < CACHE_TTL_SECONDS:
        return _cache["data"]

    grid = _build_grid()
    lats = ",".join(f"{lat:.4f}" for lat, _ in grid)
    lons = ",".join(f"{lon:.4f}" for _, lon in grid)

    resp = requests.get(
        OPEN_METEO_URL,
        params={
            "latitude": lats,
            "longitude": lons,
            "hourly": "precipitation",
            "past_days": 1,
            "forecast_days": 1,
            "timezone": "UTC",
        },
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json()

    now_utc = datetime.now(timezone.utc)
    window_start = now_utc - timedelta(hours=24)

    points = []
    for (lat, lon), station in zip(grid, results):
        times = station["hourly"]["time"]
        precip = station["hourly"]["precipitation"]
        total = 0.0
        for t_str, mm in zip(times, precip):
            t = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
            if window_start <= t <= now_utc and mm is not None:
                total += mm
        points.append({"lat": lat, "lon": lon, "mm_24h": round(total, 2)})

    basin_average = sum(p["mm_24h"] for p in points) / len(points) if points else 0.0

    data = {
        "points": points,
        "basin_average_mm": round(basin_average, 2),
        "fetched_at": now_utc.isoformat(),
        "source": "Open-Meteo (modelo meteorológico, no medición directa — ver notas del proyecto)",
    }
    _cache["timestamp"] = now
    _cache["data"] = data
    return data


_forecast_cache = {"timestamp": 0, "data": None}
FORECAST_DAYS = 7


def fetch_forecast_7d(force: bool = False) -> dict:
    """Pronóstico de lluvia diaria (promedio de cuenca) para los próximos 7 días.

    SUPUESTO NO VERIFICADO: pronóstico de modelo meteorológico (Open-Meteo),
    no hay forma de "medir" lluvia futura — la incertidumbre crece mucho día
    a día, especialmente después de 3-4 días. Tratar como ilustrativo.
    """
    now = time.time()
    if not force and _forecast_cache["data"] is not None and (now - _forecast_cache["timestamp"]) < CACHE_TTL_SECONDS:
        return _forecast_cache["data"]

    grid = _build_grid()
    lats = ",".join(f"{lat:.4f}" for lat, _ in grid)
    lons = ",".join(f"{lon:.4f}" for _, lon in grid)

    resp = requests.get(
        OPEN_METEO_URL,
        params={
            "latitude": lats,
            "longitude": lons,
            "daily": "precipitation_sum",
            "forecast_days": FORECAST_DAYS,
            "timezone": "UTC",
        },
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json()

    n_days = len(results[0]["daily"]["time"])
    day_totals = [0.0] * n_days
    dates = results[0]["daily"]["time"]
    for station in results:
        for i, mm in enumerate(station["daily"]["precipitation_sum"]):
            day_totals[i] += mm or 0.0

    days = [
        {"date": dates[i], "mm": round(day_totals[i] / len(results), 2)}
        for i in range(n_days)
    ]

    data = {
        "days": days,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "Open-Meteo (pronóstico de modelo, no medición — incertidumbre alta más allá de 3-4 días)",
    }
    _forecast_cache["timestamp"] = now
    _forecast_cache["data"] = data
    return data
