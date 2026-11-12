"""
Delimita sub-cuencas dentro de la cuenca del Limarí usando HydroBASINS a un
nivel Pfafstetter más fino que el usado para el contorno general (nivel 7 en
scripts/01_delimit_basin.py).

SUPUESTO NO VERIFICADO: HydroBASINS no trae nombres, así que estas
sub-cuencas NO están identificadas como "Río Grande", "Río Hurtado", etc. —
son solo la subdivisión hidrográfica automática de nivel Pfafstetter 9 dentro
del mismo MAIN_BAS de la cuenca del Limarí. Asignarles nombre de tributario
requeriría cruzarlas visualmente contra un mapa con nombres oficiales (DGA/
IDE Chile), que no se hizo.
"""

import json
import zipfile
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Point

ROOT = Path(__file__).resolve().parent.parent
HYDRO_DIR = ROOT / "data" / "hydro"
ZIP_PATH = HYDRO_DIR / "raw" / "hybas_sa_lev01-12_v1c.zip"

PFAF_LEVEL = 9  # más fino que el nivel 7 usado para el contorno general
# mismos puntos ancla que 01_delimit_basin.py (Elqui, Limarí, Choapa)
ANCHORS = {
    "Elqui": (-70.7081, -30.0319),
    "Limarí": (-71.2003, -30.5983),
    "Choapa": (-70.9662, -31.7783),
}


def load_level_layer(level: int) -> gpd.GeoDataFrame:
    shp_name = f"hybas_sa_lev{level:02d}_v1c.shp"
    with zipfile.ZipFile(ZIP_PATH) as zf:
        members = [n for n in zf.namelist() if n.endswith(shp_name)]
        if not members:
            raise FileNotFoundError(f"No se encontró {shp_name} en el zip.")
    return gpd.read_file(f"zip://{ZIP_PATH}!{members[0]}")


def process():
    if not ZIP_PATH.exists():
        raise SystemExit(
            f"No existe {ZIP_PATH}. Corre primero scripts/01_delimit_basin.py "
            "(descarga el mismo zip de HydroBASINS)."
        )

    print(f"Cargando capa nivel Pfafstetter {PFAF_LEVEL}...")
    gdf = load_level_layer(PFAF_LEVEL)

    partes = []
    for cuenca, (lon, lat) in ANCHORS.items():
        containing = gdf[gdf.contains(Point(lon, lat))]
        if containing.empty:
            raise RuntimeError(f"El ancla de {cuenca} no cae dentro de ningún polígono.")
        main_bas = containing.iloc[0]["MAIN_BAS"]
        sub = gdf[gdf["MAIN_BAS"] == main_bas][["HYBAS_ID", "UP_AREA", "geometry"]].copy()
        sub["cuenca"] = cuenca
        print(f"  {cuenca}: {len(sub)} sub-cuencas")
        partes.append(sub)

    import pandas as pd

    subbasins = gpd.GeoDataFrame(pd.concat(partes, ignore_index=True), crs=gdf.crs)
    subbasins["sub_id"] = range(1, len(subbasins) + 1)
    print(f"Sub-cuencas nivel {PFAF_LEVEL} (total): {len(subbasins)}")

    out_path = HYDRO_DIR / "subbasins.geojson"
    subbasins.to_file(out_path, driver="GeoJSON")
    print(f"Guardado: {out_path}")


if __name__ == "__main__":
    process()
