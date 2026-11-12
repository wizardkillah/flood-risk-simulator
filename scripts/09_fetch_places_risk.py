"""
Lugares poblados de la Región de Coquimbo (OSM) + clasificación de riesgo de
inundación por cercanía a cursos de agua.

1. Descarga de Overpass todos los place=city/town/village/hamlet del bbox
   regional (con población si está etiquetada).
2. Para cada lugar calcula la distancia al curso de agua más cercano de la
   red OSM (osm_waterways.geojson, correr scripts/08 antes), separando ríos
   permanentes de quebradas intermitentes.
3. Clasifica riesgo:
   - "alto":  a <200m de un río/estero, o <120m de una quebrada intermitente
              (los asentamientos pegados al cauce — lo primero que se moja).
   - "medio": a <600m de un curso de agua.
   - "bajo":  el resto.

LIMITACIÓN HONESTA: es un proxy geométrico plano (distancia horizontal), no
usa elevación relativa al cauce ni tamaño de la crecida. Un pueblo en un
barranco alto a 150m del río queda "alto" aunque no se inunde nunca, y uno a
700m en una planicie de inundación real queda "bajo". Sirve para priorizar
dónde mirar, no como mapa de riesgo oficial. La mejora natural es cruzar con
HAND (pendiente en Fase 2, requiere HAND regional).

Guarda: data/hydro/places_risk.geojson
"""

import importlib
import json
import sys
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Point

sys.path.insert(0, str(Path(__file__).resolve().parent))
osm = importlib.import_module("08_fetch_osm_hydro")

ROOT = Path(__file__).resolve().parent.parent
HYDRO_DIR = ROOT / "data" / "hydro"
WATERWAYS_PATH = HYDRO_DIR / "osm_waterways.geojson"
OUT_PATH = HYDRO_DIR / "places_risk.geojson"

UTM = "EPSG:32719"  # UTM 19S — metros reales para las distancias

PLACES_QUERY = f"""
[out:json][timeout:60];
node["place"~"^(city|town|village|hamlet)$"]({osm.BBOX});
out;
"""

RIESGO_ALTO_RIO_M = 200
RIESGO_ALTO_QUEBRADA_M = 120
RIESGO_MEDIO_M = 600


def fetch_places() -> gpd.GeoDataFrame:
    data = osm.overpass(PLACES_QUERY)
    rows = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        rows.append(
            {
                "geometry": Point(el["lon"], el["lat"]),
                "nombre": tags.get("name", "(sin nombre)"),
                "tipo": tags.get("place", ""),
                "poblacion": int(tags["population"]) if tags.get("population", "").isdigit() else None,
            }
        )
    return gpd.GeoDataFrame(rows, crs="EPSG:4326")


def classify_risk(places: gpd.GeoDataFrame, waterways: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    places_utm = places.to_crs(UTM)
    ww_utm = waterways.to_crs(UTM)

    # cauces naturales solamente (los canales de riego no generan crecidas)
    natural = ww_utm[ww_utm["tipo"].isin(["river", "stream"])]
    permanentes = natural[~natural["intermitente"]]
    intermitentes = natural[natural["intermitente"]]

    perm_union = permanentes.geometry.union_all() if len(permanentes) else None
    inter_union = intermitentes.geometry.union_all() if len(intermitentes) else None

    riesgos, d_perm_list, d_inter_list = [], [], []
    for geom in places_utm.geometry:
        d_perm = geom.distance(perm_union) if perm_union is not None else 1e9
        d_inter = geom.distance(inter_union) if inter_union is not None else 1e9
        if d_perm < RIESGO_ALTO_RIO_M or d_inter < RIESGO_ALTO_QUEBRADA_M:
            riesgo = "alto"
        elif min(d_perm, d_inter) < RIESGO_MEDIO_M:
            riesgo = "medio"
        else:
            riesgo = "bajo"
        riesgos.append(riesgo)
        d_perm_list.append(round(d_perm))
        d_inter_list.append(round(d_inter))

    places = places.copy()
    places["riesgo"] = riesgos
    places["dist_rio_m"] = d_perm_list
    places["dist_quebrada_m"] = d_inter_list
    return places


def main():
    if not WATERWAYS_PATH.exists():
        raise SystemExit("Falta osm_waterways.geojson — corre scripts/08_fetch_osm_hydro.py primero.")

    print("Descargando lugares poblados de OSM...")
    places = fetch_places()
    print(f"  {len(places)} lugares en el bbox regional")

    waterways = gpd.read_file(WATERWAYS_PATH)
    print("Clasificando riesgo por cercanía a cauces...")
    places = classify_risk(places, waterways)

    counts = places["riesgo"].value_counts().to_dict()
    print(f"  riesgo: {counts}")
    top = places[places["riesgo"] == "alto"].sort_values("poblacion", na_position="last", ascending=False)
    print("  lugares de riesgo alto más poblados:")
    for _, row in top.head(12).iterrows():
        pob = f"{row['poblacion']:,}" if row["poblacion"] else "s/d"
        print(f"    {row['nombre']} ({row['tipo']}, pob {pob}) — río a {row['dist_rio_m']}m, quebrada a {row['dist_quebrada_m']}m")

    places.to_file(OUT_PATH, driver="GeoJSON")
    print(f"Guardado: {OUT_PATH}")


if __name__ == "__main__":
    main()
