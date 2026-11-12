"""
Estimación gruesa de zona inundable a partir de lluvia caída, usando el
raster HAND precalculado (scripts/05_compute_hand.py).

Metodología (todo esto son SUPUESTOS FUERTES, no un modelo hidrológico
calibrado — ver README):
1. Lluvia acumulada 24h (promedio de la cuenca, de Open-Meteo) x coeficiente
   de escorrentía fijo -> volumen de escorrentía.
2. Se busca (búsqueda binaria) el alza de nivel `delta_h` tal que el
   volumen que HAND<=delta_h "contendría" dentro de la cuenca sea igual al
   volumen de escorrentía estimado.
3. La mancha de inundación es simplemente las celdas con HAND<=delta_h
   dentro de la cuenca.

Esto ignora por completo: tránsito de la onda de crecida en el tiempo,
infiltración variable, capacidad real del cauce, embalses (que retienen
agua), y cualquier calibración contra caudales históricos. Es una
ilustración rápida de "dónde podría subir el agua", no una predicción.
"""

from pathlib import Path

import numpy as np
import rasterio
import geopandas as gpd
from rasterio import features
from shapely.geometry import shape, mapping, MultiPolygon
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parent.parent
FLOOD_DIR = ROOT / "data" / "flood"
HAND_PATH = FLOOD_DIR / "hand.tif"
BASIN_PATH = ROOT / "data" / "hydro" / "limari_basin.geojson"

RUNOFF_COEFFICIENT = 0.3  # SUPUESTO NO VERIFICADO — ver README
MAX_DELTA_H_M = 30.0  # techo de búsqueda, evita extrapolar sin límite
BISECTION_TOLERANCE = 0.05  # fracción de error de volumen aceptable
MAX_ITERATIONS = 30

_state = {}
# Caché LRU por delta_h redondeado a 0.1 m. El cálculo _build_result
# (rasterización + polygonize + simplify + reproyección) toma varios segundos
# en la cuenca regional — con el slider moviéndose por decenas de posiciones
# es donde se caía la latencia. Con caché la segunda pasada por el mismo
# nivel es instantánea (<10ms).
_MASK_CACHE_MAX = 64
_mask_result_cache: "dict[float, dict]" = {}


def _load():
    if _state:
        return
    with rasterio.open(HAND_PATH) as src:
        hand = src.read(1)
        transform = src.transform
        crs = src.crs
        nodata = src.nodata

    basin = gpd.read_file(BASIN_PATH).to_crs(crs)
    basin_mask = features.rasterize(
        [(geom, 1) for geom in basin.geometry],
        out_shape=hand.shape,
        transform=transform,
        fill=0,
        dtype="uint8",
    ).astype(bool)

    valid = (hand != nodata) & basin_mask
    cell_area_m2 = abs(transform.a * transform.e)

    _state.update(
        hand=hand,
        transform=transform,
        crs=crs,
        valid=valid,
        cell_area_m2=cell_area_m2,
        basin_area_m2=float(valid.sum()) * cell_area_m2,
    )


def _flood_volume(delta_h: float) -> float:
    hand = _state["hand"]
    valid = _state["valid"]
    depth = np.where(valid & (hand <= delta_h), delta_h - hand, 0.0)
    return float(depth.sum()) * _state["cell_area_m2"]


def _flood_mask(delta_h: float) -> np.ndarray:
    hand = _state["hand"]
    valid = _state["valid"]
    return valid & (hand <= delta_h)


def _mask_to_geojson(mask: np.ndarray) -> dict:
    transform = _state["transform"]
    crs = _state["crs"]
    shapes_gen = features.shapes(mask.astype("uint8"), mask=mask, transform=transform)
    polygons = [shape(geom) for geom, val in shapes_gen if val == 1]
    if not polygons:
        return {"type": "FeatureCollection", "features": []}

    merged = unary_union(polygons)

    # Descartar fragmentos minúsculos (parches aislados de pocas celdas) y
    # simplificar: la rasterización a 150m produce miles de vértices en
    # escalera que no aportan precisión real, cuelgan el render en Cesium,
    # y sugieren una falsa precisión que esta estimación no tiene.
    min_area_m2 = 10 * _state["cell_area_m2"]
    parts = merged.geoms if isinstance(merged, MultiPolygon) else [merged]
    kept = [p for p in parts if p.area >= min_area_m2]
    if not kept:
        return {"type": "FeatureCollection", "features": []}
    merged = unary_union(kept).simplify(250, preserve_topology=True)

    gdf = gpd.GeoDataFrame(geometry=[merged], crs=crs).to_crs("EPSG:4326")
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {}, "geometry": mapping(gdf.geometry.iloc[0])}
        ],
    }


def _solve_delta_h_for_volume(runoff_volume_m3: float) -> tuple[float, bool]:
    """Bisección: delta_h tal que el volumen contenido bajo HAND<=delta_h
    iguale runoff_volume_m3. Devuelve (delta_h, capped)."""
    if runoff_volume_m3 <= 0:
        return 0.0, False
    if _flood_volume(MAX_DELTA_H_M) < runoff_volume_m3:
        return MAX_DELTA_H_M, True

    lo, hi = 0.0, MAX_DELTA_H_M
    delta_h = MAX_DELTA_H_M / 2
    for _ in range(MAX_ITERATIONS):
        vol = _flood_volume(delta_h)
        error = (vol - runoff_volume_m3) / max(runoff_volume_m3, 1.0)
        if abs(error) < BISECTION_TOLERANCE:
            break
        if vol > runoff_volume_m3:
            hi = delta_h
        else:
            lo = delta_h
        delta_h = (lo + hi) / 2
    return delta_h, False


def _build_result(delta_h: float, extra: dict) -> dict:
    # cachear por delta_h redondeado — el usuario cambia el slider en pasos
    # de 0.1m y muchas requests seguidas piden el mismo nivel
    key = round(float(delta_h), 1)
    base = _mask_result_cache.get(key)
    if base is None:
        mask = _flood_mask(key)
        flooded_area_km2 = float(mask.sum()) * _state["cell_area_m2"] / 1e6
        geojson = _mask_to_geojson(mask)
        base = {
            "delta_h_m": key,
            "flooded_area_km2": round(flooded_area_km2, 2),
            "flooded_pct_of_basin": round(100 * flooded_area_km2 / (_state["basin_area_m2"] / 1e6), 2),
            "geojson": geojson,
            "disclaimer": (
                "Estimación gruesa no calibrada. NO usar para decisiones de evacuación reales."
            ),
        }
        if len(_mask_result_cache) >= _MASK_CACHE_MAX:
            _mask_result_cache.pop(next(iter(_mask_result_cache)))  # LRU-ish
        _mask_result_cache[key] = base
    result = dict(base)  # copia superficial para que 'extra' (mode, etc.) no contamine el caché
    result.update(extra)
    return result


def estimate_flood(rainfall_mm: float, runoff_coefficient: float = RUNOFF_COEFFICIENT) -> dict:
    """Modo automático: nivel derivado de la lluvia caída (ver metodología en README)."""
    _load()
    runoff_volume_m3 = (rainfall_mm / 1000.0) * _state["basin_area_m2"] * runoff_coefficient
    delta_h, capped = _solve_delta_h_for_volume(runoff_volume_m3)
    return _build_result(delta_h, {
        "mode": "auto",
        "rainfall_mm_24h": round(rainfall_mm, 1),
        "runoff_coefficient": runoff_coefficient,
        "runoff_volume_m3": round(runoff_volume_m3),
        "delta_h_capped": capped,
    })


def estimate_flood_at_level(delta_h: float) -> dict:
    """Modo manual: el usuario fija directamente el alza de nivel (slider)."""
    _load()
    delta_h = max(0.0, min(delta_h, MAX_DELTA_H_M))
    return _build_result(delta_h, {"mode": "manual"})
