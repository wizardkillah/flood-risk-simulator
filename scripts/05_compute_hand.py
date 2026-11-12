"""
Calcula el raster HAND (Height Above Nearest Drainage) de la cuenca del
Limarí a partir del DEM. HAND[celda] = cuánto tiene que subir el agua en la
celda más cercana de la red de drenaje para que esa celda se inunde.

Es la base del cálculo de "zona inundable estimada" del prototipo: la lluvia
determina un volumen de escorrentía; ese volumen se traduce en un delta_h
(alza de nivel) buscando qué delta_h inunda (según HAND) un volumen
equivalente. Ver scripts/06_estimate_flood.py.

SUPUESTOS NO VERIFICADOS:
- Se reproyecta a UTM 19S (EPSG:32719) y se remuestrea a ~150m de resolución
  (no los ~30m nativos del DEM) para que el cálculo sea rápido. Suficiente
  para una estimación gruesa, no para ingeniería de detalle.
- La red de drenaje se define por un umbral arbitrario de celdas de
  acumulación de flujo (STREAM_THRESHOLD) — no se validó contra el cauce
  real del río Limarí.
- pysheds 0.5 tiene un bug de compatibilidad con numpy>=2.0 (usa la función
  removida `np.in1d`); se aplica un monkeypatch (`np.in1d = np.isin`) antes
  de importar pysheds.
"""

import numpy as np

np.in1d = np.isin  # compat shim para pysheds 0.5 + numpy>=2.0

import json

import geopandas as gpd
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import calculate_default_transform, reproject
from pysheds.grid import Grid
from shapely.geometry import shape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEM_PATH = ROOT / "data" / "dem" / "limari_cop30.tif"
FLOOD_DIR = ROOT / "data" / "flood"

UTM_CRS = "EPSG:32719"  # WGS84 / UTM zone 19S — cubre la cuenca del Limarí
TARGET_RES_M = 150  # resolución del raster de trabajo, en metros
STREAM_THRESHOLD = 10000  # celdas de acumulación para considerar "cauce" (~225km² aguas arriba)


def reproject_and_downsample():
    with rasterio.open(DEM_PATH) as src:
        transform, width, height = calculate_default_transform(
            src.crs, UTM_CRS, src.width, src.height, *src.bounds,
            resolution=TARGET_RES_M,
        )
        profile = src.profile.copy()
        profile.update(crs=UTM_CRS, transform=transform, width=width, height=height, dtype="float64")

        out_path = FLOOD_DIR / "dem_utm150m.tif"
        FLOOD_DIR.mkdir(parents=True, exist_ok=True)
        with rasterio.open(out_path, "w", **profile) as dst:
            reproject(
                source=rasterio.band(src, 1),
                destination=rasterio.band(dst, 1),
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=transform,
                dst_crs=UTM_CRS,
                resampling=Resampling.average,
            )
    print(f"DEM reproyectado a {UTM_CRS} @ {TARGET_RES_M}m: {height}x{width} -> {out_path}")
    return out_path


def compute_hand(dem_path):
    grid = Grid.from_raster(str(dem_path))
    dem = grid.read_raster(str(dem_path))

    pit_filled = grid.fill_pits(dem)
    flooded = grid.fill_depressions(pit_filled)
    inflated = grid.resolve_flats(flooded)

    fdir = grid.flowdir(inflated)
    acc = grid.accumulation(fdir)
    stream_mask = acc > STREAM_THRESHOLD
    print(f"Celdas de cauce: {int(stream_mask.sum())} de {stream_mask.size} "
          f"({100 * stream_mask.sum() / stream_mask.size:.2f}%)")

    hand = grid.compute_hand(fdir, inflated, stream_mask)
    hand_arr = np.asarray(hand, dtype="float32")
    finite = np.isfinite(hand_arr)
    print(f"HAND: min={hand_arr[finite].min():.1f}m max={hand_arr[finite].max():.1f}m")

    river_network = grid.extract_river_network(fdir, stream_mask)
    print(f"Segmentos de río extraídos: {len(river_network['features'])}")

    return grid, inflated, hand_arr, stream_mask, river_network


def save_river_network(river_network, crs):
    # Descartar segmentos degenerados (1 solo punto) que a veces salen de
    # extract_river_network en los bordes del grid.
    features = [f for f in river_network["features"] if len(f["geometry"]["coordinates"]) >= 2]
    geoms = [shape(f["geometry"]) for f in features]
    gdf = gpd.GeoDataFrame(geometry=geoms, crs=crs).to_crs("EPSG:4326")
    out_path = FLOOD_DIR / "river_network.geojson"
    gdf.to_file(out_path, driver="GeoJSON")
    print(f"Guardado: {out_path} ({len(gdf)} segmentos)")


def process():
    dem_path = reproject_and_downsample()
    grid, dem, hand_arr, stream_mask, river_network = compute_hand(dem_path)

    with rasterio.open(dem_path) as src:
        profile = src.profile.copy()
        crs = src.crs

    save_river_network(river_network, crs)

    profile.update(dtype="float32", nodata=-9999.0)
    hand_out = np.where(np.isfinite(hand_arr), hand_arr, -9999.0).astype("float32")
    hand_path = FLOOD_DIR / "hand.tif"
    with rasterio.open(hand_path, "w", **profile) as dst:
        dst.write(hand_out, 1)
    print(f"Guardado: {hand_path}")

    profile_mask = profile.copy()
    profile_mask.update(dtype="uint8", nodata=0)
    stream_path = FLOOD_DIR / "stream_mask.tif"
    with rasterio.open(stream_path, "w", **profile_mask) as dst:
        dst.write(stream_mask.astype("uint8"), 1)
    print(f"Guardado: {stream_path}")


if __name__ == "__main__":
    process()
