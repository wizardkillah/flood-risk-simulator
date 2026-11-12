"""
Convierte el DEM de la cuenca del Limarí en un set de teselas de terreno
quantized-mesh que CesiumJS puede consumir directamente (self-hosted, sin
Cesium ion).

Pipeline: DEM (GeoTIFF) -> grilla regular por tesela -> malla (pymartini)
-> teselas quantized-mesh (quantized-mesh-encoder) -> layer.json

SUPUESTOS NO VERIFICADOS:
- Las alturas del DEM (COP30) están referenciadas al geoide EGM2008, no a la
  elipsoide WGS84 que usa Cesium internamente. No se aplica corrección de
  datum en este prototipo (la diferencia típica son ~30-40m en esta zona) —
  el relieve se ve correcto, pero la altura absoluta puede estar desplazada.
  Corregir antes de cualquier cálculo de cota de inundación real en Fase 2.
- Celdas sin dato (océano/nodata) se rellenan con altura 0.
"""

import gzip
import json
import math
from pathlib import Path

import numpy as np
import quantized_mesh_encoder
import rasterio
from rasterio.windows import Window, from_bounds
from pymartini import Martini

ROOT = Path(__file__).resolve().parent.parent
DEM_PATH = ROOT / "data" / "dem" / "limari_cop30.tif"
OUT_DIR = ROOT / "data" / "terrain_tiles"

# Con z10 el espaciado de muestreo era ~305m — desaprovechaba 10x la
# resolución real del DEM (~30m). z12 baja a ~76m; el salto a z13 (~38m)
# multiplicaría de nuevo por 4 el número de teselas, se deja para cuando se
# valide el rendimiento con z12.
MAX_ZOOM = 12
GRID_SIZE = 65  # 2^6 + 1, tamaño de grilla de muestreo por tesela (requisito de pymartini)
MAX_MESH_ERROR = 5.0  # metros; mayor = menos triángulos = teselas más livianas


def tile_bounds(z, x, y):
    """Bounds geográficos de una tesela en esquema TMS (y=0 en el sur)."""
    num = 2 ** z
    tile_w = 360.0 / (2 * num)
    tile_h = 180.0 / num
    west = -180.0 + x * tile_w
    east = west + tile_w
    south = -90.0 + y * tile_h
    north = south + tile_h
    return west, south, east, north


def tiles_for_bbox(z, west, south, east, north):
    num = 2 ** z
    tile_w = 360.0 / (2 * num)
    tile_h = 180.0 / num
    x0 = max(0, math.floor((west + 180.0) / tile_w))
    x1 = min(2 * num - 1, math.ceil((east + 180.0) / tile_w) - 1)
    y0 = max(0, math.floor((south + 90.0) / tile_h))
    y1 = min(num - 1, math.ceil((north + 90.0) / tile_h) - 1)
    return x0, y0, x1, y1


def sample_grid(dataset, west, south, east, north, size, nodata_fill=0.0):
    """Muestrea una grilla size x size con interpolación bilineal, leyendo del
    raster solo la ventana necesaria (dataset.sample punto a punto era el
    cuello de botella al subir MAX_ZOOM). Los puntos de la grilla incluyen los
    bordes exactos de la tesela y la interpolación es determinista sobre el
    mismo raster, así las teselas vecinas comparten vértices de borde
    idénticos (sin grietas)."""
    lons = np.linspace(west, east, size)
    lats = np.linspace(north, south, size)  # fila 0 = norte

    win = from_bounds(west, south, east, north, transform=dataset.transform)
    col0 = max(int(math.floor(win.col_off)) - 1, 0)
    row0 = max(int(math.floor(win.row_off)) - 1, 0)
    col1 = min(int(math.ceil(win.col_off + win.width)) + 1, dataset.width)
    row1 = min(int(math.ceil(win.row_off + win.height)) + 1, dataset.height)
    if col1 <= col0 or row1 <= row0:
        return np.full((size, size), nodata_fill, dtype=np.float32)

    window = Window(col0, row0, col1 - col0, row1 - row0)
    data = dataset.read(1, window=window).astype(np.float64)
    if dataset.nodata is not None:
        data[data == dataset.nodata] = nodata_fill
    data[np.isnan(data)] = nodata_fill

    # Índices fraccionales de cada lon/lat objetivo dentro de la ventana leída
    # (0.5 = centro de celda). Fuera del DEM se clampa al borde (mejor que un
    # escalón a 0 en las teselas del borde de la cuenca).
    t = dataset.window_transform(window)
    cols = np.clip((lons - t.c) / t.a - 0.5, 0.0, data.shape[1] - 1.0)
    rows = np.clip((lats - t.f) / t.e - 0.5, 0.0, data.shape[0] - 1.0)
    c0 = np.floor(cols).astype(int)
    r0 = np.floor(rows).astype(int)
    c1 = np.minimum(c0 + 1, data.shape[1] - 1)
    r1 = np.minimum(r0 + 1, data.shape[0] - 1)
    fc = (cols - c0)[None, :]
    fr = (rows - r0)[:, None]
    top = data[r0[:, None], c0[None, :]] * (1 - fc) + data[r0[:, None], c1[None, :]] * fc
    bot = data[r1[:, None], c0[None, :]] * (1 - fc) + data[r1[:, None], c1[None, :]] * fc
    return (top * (1 - fr) + bot * fr).astype(np.float32)


def generate_tile(dataset, martini, z, x, y):
    west, south, east, north = tile_bounds(z, x, y)
    terrain = sample_grid(dataset, west, south, east, north, GRID_SIZE)

    tile = martini.create_tile(terrain)
    raw_vertices, raw_triangles = tile.get_mesh(MAX_MESH_ERROR)

    # pymartini.get_mesh devuelve índices de grilla (col, row), no alturas —
    # hay que resolver lon/lat/altura a partir de esos índices y `terrain`.
    grid_xy = raw_vertices.reshape(-1, 2)
    grid_x, grid_y = grid_xy[:, 0], grid_xy[:, 1]
    lons = west + (grid_x / (GRID_SIZE - 1)) * (east - west)
    lats = north - (grid_y / (GRID_SIZE - 1)) * (north - south)
    heights = terrain[grid_y, grid_x]
    mesh_vertices = np.column_stack([lons, lats, heights]).astype(np.float64)
    triangles = raw_triangles.reshape(-1, 3)

    out_dir = OUT_DIR / str(z) / str(x)
    out_dir.mkdir(parents=True, exist_ok=True)
    file_path = out_dir / f"{y}.terrain"
    with gzip.open(file_path, "wb") as f:
        quantized_mesh_encoder.encode(
            f, mesh_vertices, triangles, bounds=(west, south, east, north)
        )
    return file_path


def process_terrain():
    if not DEM_PATH.exists():
        raise SystemExit(f"No existe {DEM_PATH}. Corre primero scripts/02_download_dem.py")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    martini = Martini(GRID_SIZE)

    with rasterio.open(DEM_PATH) as dataset:
        dem_west, dem_south, dem_east, dem_north = dataset.bounds

        available = []
        total_tiles = 0
        levels = []
        for z in range(0, MAX_ZOOM + 1):
            x0, y0, x1, y1 = tiles_for_bbox(z, dem_west, dem_south, dem_east, dem_north)
            levels.append((z, x0, y0, x1, y1))
            # Los archivos en disco usan Y estilo TMS (0 = sur), pero el árbol de
            # "available" de layer.json lo interpreta Cesium con Y nativo (0 = norte)
            # sin importar el flag "scheme": hay que invertir el eje Y aquí.
            num_y = 2 ** z
            native_y0 = num_y - 1 - y1
            native_y1 = num_y - 1 - y0
            available.append([{"startX": x0, "startY": native_y0, "endX": x1, "endY": native_y1}])
            n_tiles = (x1 - x0 + 1) * (y1 - y0 + 1)
            total_tiles += n_tiles
            print(f"Nivel {z}: teselas x[{x0}-{x1}] y[{y0}-{y1}] ({n_tiles} teselas)")

        print(f"Total de teselas a generar: {total_tiles}")

        count = 0
        for z, x0, y0, x1, y1 in levels:
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    generate_tile(dataset, martini, z, x, y)
                    count += 1
                    if count % 20 == 0 or count == total_tiles:
                        print(f"  {count}/{total_tiles} teselas generadas...")

    layer = {
        "tilejson": "2.1.0",
        "format": "quantized-mesh-1.0",
        "version": "1.0.0",
        "scheme": "tms",
        "tiles": ["{z}/{x}/{y}.terrain?v={version}"],
        "projection": "EPSG:4326",
        "bounds": [dem_west, dem_south, dem_east, dem_north],
        "available": available,
        "attribution": "DEM: Copernicus GLO-30 (OpenTopography). Procesado localmente — prototipo, no oficial.",
    }
    with open(OUT_DIR / "layer.json", "w", encoding="utf-8") as f:
        json.dump(layer, f, indent=2)

    print(f"\n{count} teselas generadas en {OUT_DIR}")
    print(f"layer.json escrito en {OUT_DIR / 'layer.json'}")


if __name__ == "__main__":
    process_terrain()
