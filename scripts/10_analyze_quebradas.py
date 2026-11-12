"""
Análisis de quebradas de activación rara y sus puntos de descarga a los ríos.

Trabaja sobre la red OSM regional (scripts/08):

1. **Quebradas intermitentes** (`intermittent=yes` en OSM): cauces que pasan
   secos la mayor parte del tiempo y solo llevan agua con lluvia intensa —
   exactamente los que la población olvida y donde se construye/estaciona/
   cruza, y los que producen los daños más sorpresivos (aluviones de 2015,
   2017 y el temporal de julio 2026). Se separan como capa propia.

2. **Puntos de descarga** ("caídas de agua que llegan a los ríos"): las
   confluencias donde una quebrada intermitente desemboca en un cauce
   permanente. Se calculan buscando el extremo de cada quebrada que queda a
   <150m de un río/estero permanente. Cada punto lleva el nombre de la
   quebrada (si tiene) y del río receptor — son los lugares donde una
   activación repentina inyecta caudal y sedimento al río principal.

LIMITACIÓN: el etiquetado `intermittent` de OSM es incompleto — una quebrada
sin la etiqueta cuenta como permanente aunque no lo sea. Los conteos son piso,
no techo.

Guarda:
  data/hydro/quebradas_intermitentes.geojson  (LineStrings)
  data/hydro/confluencias.geojson             (Points)
"""

from pathlib import Path

import geopandas as gpd
from shapely.geometry import Point

ROOT = Path(__file__).resolve().parent.parent
HYDRO_DIR = ROOT / "data" / "hydro"
WATERWAYS_PATH = HYDRO_DIR / "osm_waterways.geojson"
OUT_QUEBRADAS = HYDRO_DIR / "quebradas_intermitentes.geojson"
OUT_CONFLUENCIAS = HYDRO_DIR / "confluencias.geojson"

UTM = "EPSG:32719"
SNAP_M = 150  # distancia máx del extremo de la quebrada al río receptor


def main():
    ww = gpd.read_file(WATERWAYS_PATH)
    natural = ww[ww["tipo"].isin(["river", "stream"])]

    intermitentes = natural[natural["intermitente"]].copy()
    permanentes = natural[~natural["intermitente"]].copy()
    print(f"Cauces naturales: {len(natural)} — intermitentes: {len(intermitentes)}, permanentes: {len(permanentes)}")

    intermitentes.to_file(OUT_QUEBRADAS, driver="GeoJSON")
    print(f"Guardado: {OUT_QUEBRADAS}")

    # --- confluencias: extremo de quebrada intermitente cerca de cauce permanente ---
    inter_utm = intermitentes.to_crs(UTM)
    perm_utm = permanentes.to_crs(UTM)
    perm_union = perm_utm.geometry.union_all()

    rows = []
    for _, q in inter_utm.iterrows():
        geom = q.geometry
        if geom.geom_type == "MultiLineString":
            parts = list(geom.geoms)
        else:
            parts = [geom]
        for part in parts:
            for endpoint in (Point(part.coords[0]), Point(part.coords[-1])):
                d = endpoint.distance(perm_union)
                if d < SNAP_M:
                    rows.append(
                        {
                            "geometry": endpoint,
                            "quebrada": q["nombre"] or "(quebrada sin nombre)",
                            "dist_m": round(d),
                        }
                    )

    if not rows:
        print("Sin confluencias detectadas.")
        conf = gpd.GeoDataFrame({"geometry": []}, crs=UTM)
    else:
        conf = gpd.GeoDataFrame(rows, crs=UTM)
        # nombre del río receptor via join espacial al cauce permanente más cercano
        # (sjoin_nearest duplica filas ante empates de distancia: dedup por índice)
        joined = gpd.sjoin_nearest(conf, perm_utm[["nombre", "geometry"]], how="left", distance_col="d_rec")
        joined = joined[~joined.index.duplicated(keep="first")]
        conf["rio_receptor"] = joined.loc[conf.index, "nombre"].fillna("").replace("", "(sin nombre)")
        # dedup: varias quebradas convergen en el mismo nodo — quedarse con puntos únicos a <50m
        conf = conf.to_crs("EPSG:4326")
        conf["_key"] = conf.geometry.apply(lambda g: (round(g.x, 4), round(g.y, 4)))
        conf = conf.drop_duplicates("_key").drop(columns="_key")

    conf.to_file(OUT_CONFLUENCIAS, driver="GeoJSON")
    print(f"Confluencias quebrada→río: {len(conf)}")
    print(f"Guardado: {OUT_CONFLUENCIAS}")

    con_nombre = conf[conf["quebrada"] != "(quebrada sin nombre)"] if len(conf) else conf
    if len(con_nombre):
        print("Ejemplos con nombre:")
        for _, r in con_nombre.head(10).iterrows():
            print(f"  {r['quebrada']} -> {r['rio_receptor']}")


if __name__ == "__main__":
    main()
