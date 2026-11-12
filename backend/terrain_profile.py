"""
Recorta un parche local del DEM real (alta resolución, ~30m) alrededor de un
punto del río, para la ventana "corte de terreno con agua subiendo" del
visor (selecciona un tramo de río -> ver el relieve real ahí y simular el
nivel de agua).

SUPUESTO: la "altura del cauce" de referencia se toma como la elevación
mínima dentro del parche recortado (razonable si el parche está centrado en
o cerca del río: el punto más bajo del entorno inmediato debería ser el
cauce). No usa el HAND real de scripts/05_compute_hand.py porque ese está en
una grilla de 150m, demasiado gruesa para un corte local de unos pocos km.
"""

from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import from_bounds

ROOT = Path(__file__).resolve().parent.parent
DEM_PATH = ROOT / "data" / "dem" / "limari_cop30.tif"

# 128 muestras sobre 3km ≈ 23m entre puntos — aprovecha toda la resolución
# del DEM (~30m). Con 64 quedaba en ~47m y el relieve se veía suavizado.
GRID_N = 128
METERS_PER_DEG_LAT = 111_320.0


def get_terrain_profile(lat: float, lon: float, size_km: float = 3.0) -> dict:
    half_deg_lat = (size_km * 1000 / 2) / METERS_PER_DEG_LAT
    meters_per_deg_lon = METERS_PER_DEG_LAT * np.cos(np.radians(lat))
    half_deg_lon = (size_km * 1000 / 2) / meters_per_deg_lon

    west, east = lon - half_deg_lon, lon + half_deg_lon
    south, north = lat - half_deg_lat, lat + half_deg_lat

    with rasterio.open(DEM_PATH) as src:
        window = from_bounds(west, south, east, north, transform=src.transform)
        data = src.read(
            1,
            window=window,
            out_shape=(GRID_N, GRID_N),
            resampling=Resampling.bilinear,
        ).astype("float64")
        if src.nodata is not None:
            data[data == src.nodata] = np.nan

    if np.all(np.isnan(data)):
        raise ValueError("El parche recortado no tiene datos de elevación válidos.")

    data = np.nan_to_num(data, nan=float(np.nanmin(data)))

    return {
        "grid_n": GRID_N,
        "size_km": size_km,
        "heights": data.flatten().round(1).tolist(),
        "min_elev_m": round(float(data.min()), 1),
        "max_elev_m": round(float(data.max()), 1),
        "channel_elev_m": round(float(data.min()), 1),
        "center": {"lat": lat, "lon": lon},
        # bounds del parche, para drapear imagería satelital encima en el visor
        "bounds": {"west": west, "south": south, "east": east, "north": north},
    }
