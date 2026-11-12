"""
Delimita las tres grandes cuencas de la Región de Coquimbo — Elqui, Limarí y
Choapa — usando HydroBASINS (hydrosheds.org).

HydroBASINS no incluye nombres de cuenca, así que la selección se hace por
puntos de ancla: se ubica el polígono que contiene un punto claramente dentro
de cada cuenca y se toma su atributo MAIN_BAS, que identifica todos los
sub-polígonos que drenan al mismo punto de salida al mar. Cada cuenca se
disuelve en un polígono con su nombre.

SUPUESTOS NO VERIFICADOS (revisar con datos oficiales DGA/IDE Chile):
- Puntos de ancla: coordenadas aproximadas de centros urbanos, de memoria.
- Nivel Pfafstetter 7 (balance precisión/tamaño).
- Las cuencas costeras menores entre las tres grandes (Los Choros, Quilimarí,
  quebradas costeras de La Serena/Coquimbo) NO quedan delimitadas — el bbox
  regional sí las cubre en terreno/imagería/red OSM, pero el modelo HAND solo
  opera dentro de las tres cuencas delimitadas.
"""

import json
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import Point

ROOT = Path(__file__).resolve().parent.parent
HYDRO_DIR = ROOT / "data" / "hydro"
RAW_DIR = HYDRO_DIR / "raw"

HYDROBASINS_URL = "https://data.hydrosheds.org/file/hydrobasins/standard/hybas_sa_lev01-12_v1c.zip"
ZIP_PATH = RAW_DIR / "hybas_sa_lev01-12_v1c.zip"
PFAF_LEVEL = 7  # ver supuesto no verificado arriba

# Un punto ancla dentro de cada cuenca mayor de la región (aprox, de memoria).
ANCHORS = {
    "Cuenca del Elqui": (-70.7081, -30.0319),   # Vicuña
    "Cuenca del Limarí": (-71.2003, -30.5983),  # Ovalle
    "Cuenca del Choapa": (-70.9662, -31.7783),  # Salamanca
}

# Bbox fijo que cubre TODA la Región de Coquimbo (incl. franja costera y
# cabeceras andinas) — el dominio de terreno/imagería/OSM/lluvia del visor.
REGION_BBOX = {"north": -29.0, "south": -32.35, "east": -69.75, "west": -71.80}


def download_hydrobasins():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if ZIP_PATH.exists():
        print(f"Ya existe {ZIP_PATH.name}, se omite la descarga.")
        return
    print(f"Descargando HydroBASINS Sudamérica (~330 MB) desde {HYDROBASINS_URL} ...")
    with requests.get(HYDROBASINS_URL, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(ZIP_PATH, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
    print("Descarga completa.")


def load_level_layer(level: int) -> gpd.GeoDataFrame:
    shp_name = f"hybas_sa_lev{level:02d}_v1c.shp"
    with zipfile.ZipFile(ZIP_PATH) as zf:
        members = [n for n in zf.namelist() if n.endswith(shp_name)]
        if not members:
            raise FileNotFoundError(
                f"No se encontró {shp_name} dentro del zip. "
                f"Archivos disponibles: {zf.namelist()[:10]}..."
            )
    return gpd.read_file(f"zip://{ZIP_PATH}!{members[0]}")


def delimit_basins():
    download_hydrobasins()

    print(f"Cargando capa nivel Pfafstetter {PFAF_LEVEL}...")
    gdf = load_level_layer(PFAF_LEVEL)

    basins = []
    for nombre, (lon, lat) in ANCHORS.items():
        anchor = Point(lon, lat)
        containing = gdf[gdf.contains(anchor)]
        if containing.empty:
            raise RuntimeError(f"El ancla de {nombre} no cae en ningún polígono HydroBASINS.")
        main_bas = containing.iloc[0]["MAIN_BAS"]
        parts = gdf[gdf["MAIN_BAS"] == main_bas]
        dissolved = parts.dissolve(by="MAIN_BAS").reset_index(drop=True)
        dissolved["nombre"] = nombre
        dissolved["main_bas"] = str(main_bas)
        print(f"{nombre}: MAIN_BAS {main_bas}, {len(parts)} sub-polígonos")
        basins.append(dissolved[["nombre", "main_bas", "geometry"]])

    result = gpd.GeoDataFrame(pd.concat(basins, ignore_index=True), crs=gdf.crs)

    HYDRO_DIR.mkdir(parents=True, exist_ok=True)
    # Se mantiene el nombre de archivo histórico para no romper backend/flood.py
    # y /api/basin — ahora contiene las TRES cuencas de la región.
    out_geojson = HYDRO_DIR / "limari_basin.geojson"
    result.to_file(out_geojson, driver="GeoJSON")
    print(f"Guardado: {out_geojson} ({len(result)} cuencas)")

    bbox_out = {
        "bbox_ajustado_hydrobasins": REGION_BBOX,
        "fuente": "Bbox fijo Región de Coquimbo + cuencas HydroBASINS nivel " + str(PFAF_LEVEL),
        "nota": (
            "El bbox cubre toda la región (terreno/imagería/OSM/lluvia). Las cuencas "
            "delimitadas son Elqui, Limarí y Choapa; las costeras menores no. "
            "Validar contra datos oficiales DGA/IDE Chile antes de uso operacional."
        ),
    }
    bbox_path = HYDRO_DIR / "limari_bbox.json"
    with open(bbox_path, "w", encoding="utf-8") as f:
        json.dump(bbox_out, f, indent=2, ensure_ascii=False)
    print(f"Guardado: {bbox_path}")


if __name__ == "__main__":
    delimit_basins()
