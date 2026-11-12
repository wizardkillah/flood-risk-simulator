"""
Descarga desde OpenStreetMap (Overpass API) para la cuenca del Limarí:

1. La red de cursos de agua real (waterway=river/stream/canal/drain) con sus
   nombres — el trazado oficial colaborativo de OSM, mucho más preciso que la
   red derivada del DEM por acumulación de flujo (scripts/05_compute_hand.py),
   que se mantiene solo como insumo del modelo HAND.
2. Badenes (ford=*) y pasos a nivel (railway=level_crossing) — puntos donde
   un camino cruza el agua a nivel: los primeros lugares que se cortan en una
   crecida.

Todo se recorta al polígono de la cuenca ya delimitada. Guarda:
  data/hydro/osm_waterways.geojson   (LineStrings, prop: nombre, tipo)
  data/hydro/osm_crossings.geojson   (Points, prop: nombre, tipo)

Nota: en la sesión 2026-07-18 Overpass devolvió 406 en dos servidores. Se
reintenta con User-Agent identificable (requisito de la política de uso de
varios espejos Overpass) y una lista de espejos.
"""

import json
import time
from pathlib import Path

import geopandas as gpd
import requests
from shapely.geometry import LineString, Point

ROOT = Path(__file__).resolve().parent.parent
HYDRO_DIR = ROOT / "data" / "hydro"
BASIN_PATH = HYDRO_DIR / "limari_basin.geojson"
OUT_WATERWAYS = HYDRO_DIR / "osm_waterways.geojson"
OUT_CROSSINGS = HYDRO_DIR / "osm_crossings.geojson"

# bbox de la Región de Coquimbo completa (S, W, N, E) — igual que el resto
# del pipeline desde la ampliación regional
BBOX = "-32.35,-71.80,-29.00,-69.75"

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
HEADERS = {
    "User-Agent": "simulador-crecidas-limari/0.1 (prototipo educativo Python)",
}

WATERWAYS_QUERY = f"""
[out:json][timeout:120];
way["waterway"~"^(river|stream|canal|drain)$"]({BBOX});
out geom tags;
"""

# Consultas separadas y por valor exacto: la unión con node["ford"] (por
# existencia de clave) devolvía 504 sostenido en tres espejos — esa clave no
# está indexada por valor y el servidor no la resuelve a tiempo. Con
# ford="yes" (indexada) responde en segundos. ford=stepping_stones u otros
# valores raros quedan fuera, aceptable para el prototipo.
CROSSINGS_QUERIES = {
    "baden_nodo": f'[out:json][timeout:30];node["ford"="yes"]({BBOX});out;',
    "baden_tramo": f'[out:json][timeout:30];way["ford"]({BBOX});out geom tags;',
    "paso_nivel": f'[out:json][timeout:30];node["railway"="level_crossing"]({BBOX});out;',
}


def overpass(query: str) -> dict:
    last_error = None
    for url in ENDPOINTS:
        try:
            print(f"Consultando {url} ...")
            resp = requests.post(url, data={"data": query}, headers=HEADERS, timeout=180)
            if resp.status_code == 200:
                return resp.json()
            last_error = f"{url} -> HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as exc:  # noqa: BLE001 — reintentar con el siguiente espejo
            last_error = f"{url} -> {exc}"
        print(f"  falló: {last_error}")
        time.sleep(2)
    raise SystemExit(f"Overpass falló en todos los endpoints. Último error: {last_error}")


def waterways_to_gdf(data: dict) -> gpd.GeoDataFrame:
    rows = []
    for el in data.get("elements", []):
        if el.get("type") != "way" or "geometry" not in el:
            continue
        coords = [(p["lon"], p["lat"]) for p in el["geometry"]]
        if len(coords) < 2:
            continue
        tags = el.get("tags", {})
        rows.append(
            {
                "geometry": LineString(coords),
                "nombre": tags.get("name", ""),
                "tipo": tags.get("waterway", ""),
                "intermitente": tags.get("intermittent", "") == "yes",
            }
        )
    return gpd.GeoDataFrame(rows, crs="EPSG:4326")


def crossings_to_gdf(data: dict) -> gpd.GeoDataFrame:
    rows = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        if el.get("type") == "node":
            geom = Point(el["lon"], el["lat"])
        elif el.get("type") == "way" and el.get("geometry"):
            # un badén mapeado como tramo de camino: se representa por su punto medio
            line = LineString([(p["lon"], p["lat"]) for p in el["geometry"]])
            geom = line.interpolate(0.5, normalized=True)
        else:
            continue
        tipo = "paso_nivel" if tags.get("railway") == "level_crossing" else "baden"
        rows.append(
            {
                "geometry": geom,
                "nombre": tags.get("name", ""),
                "tipo": tipo,
            }
        )
    return gpd.GeoDataFrame(rows, crs="EPSG:4326")


def main():
    # Desde la ampliación regional NO se recorta a las cuencas delimitadas:
    # las quebradas costeras de La Serena/Coquimbo (fuera de Elqui/Limarí/
    # Choapa) son precisamente zonas de riesgo real — el bbox regional es el
    # dominio completo.
    print("=== Cursos de agua (waterways) ===")
    ww = waterways_to_gdf(overpass(WATERWAYS_QUERY))
    print(f"  {len(ww)} tramos en el bbox regional")
    ww.to_file(OUT_WATERWAYS, driver="GeoJSON")
    print(f"  -> {OUT_WATERWAYS}")
    con_nombre = ww[ww["nombre"] != ""]
    print(f"  con nombre: {len(con_nombre)}")
    print(f"  por tipo: {ww['tipo'].value_counts().to_dict()}")
    print(f"  intermitentes: {int(ww['intermitente'].sum())}")

    print("=== Badenes y pasos a nivel ===")
    elements = []
    for name, q in CROSSINGS_QUERIES.items():
        data = overpass(q)
        n = len(data.get("elements", []))
        print(f"  {name}: {n} elementos")
        elements.extend(data.get("elements", []))
    elements = [e for e in elements if e.get("tags", {}).get("ford") != "no"]
    cr = crossings_to_gdf({"elements": elements})
    print(f"  {len(cr)} puntos en el bbox regional — por tipo: {cr['tipo'].value_counts().to_dict() if len(cr) else {}}")
    cr.to_file(OUT_CROSSINGS, driver="GeoJSON")
    print(f"  -> {OUT_CROSSINGS}")


if __name__ == "__main__":
    main()
