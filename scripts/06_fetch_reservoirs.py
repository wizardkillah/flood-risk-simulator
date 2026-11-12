"""
Embalses de la Región de Coquimbo con geometría real de OpenStreetMap.

Consulta Overpass por `water=reservoir` en el bbox regional (ways y
relations multipolígono) y guarda los polígonos con nombre. Esto reemplaza
el fallback anterior de círculos ilustrativos (que quedó obsoleto cuando
Overpass volvió a responder — ver historial en git).

Guarda: data/hydro/reservoirs.geojson (prop: nombre)
"""

import importlib
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Polygon
from shapely.ops import polygonize, unary_union

sys.path.insert(0, str(Path(__file__).resolve().parent))
osm = importlib.import_module("08_fetch_osm_hydro")

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "hydro" / "reservoirs.geojson"

# La Paloma y Cogotí no llevan water=reservoir en OSM — se buscan también los
# natural=water CON nombre que contenga Embalse/Tranque (el filtro por nombre
# evita traer todos los cuerpos de agua de la región).
QUERY = f"""
[out:json][timeout:120];
(
  way["water"="reservoir"]({osm.BBOX});
  relation["water"="reservoir"]({osm.BBOX});
  way["natural"="water"]["name"~"Embalse|Tranque",i]({osm.BBOX});
  relation["natural"="water"]["name"~"Embalse|Tranque",i]({osm.BBOX});
  way["landuse"="reservoir"]({osm.BBOX});
  relation["landuse"="reservoir"]({osm.BBOX});
);
out geom tags;
"""

MIN_AREA_KM2 = 0.05  # descarta tranques menores para no saturar el mapa


def element_polygons(el) -> list[Polygon]:
    if el["type"] == "way" and el.get("geometry"):
        coords = [(p["lon"], p["lat"]) for p in el["geometry"]]
        if len(coords) >= 4 and coords[0] == coords[-1]:
            return [Polygon(coords)]
        return []
    if el["type"] == "relation" and el.get("members"):
        # rol "outer" cuando existe; si el mapeo no puso roles, usar todos los ways
        outers = [
            m for m in el["members"]
            if m.get("role") == "outer" and m.get("geometry") and len(m["geometry"]) >= 2
        ]
        if not outers:
            outers = [m for m in el["members"] if m.get("geometry") and len(m["geometry"]) >= 2]
        lines = [LineString([(p["lon"], p["lat"]) for p in m["geometry"]]) for m in outers]
        if lines:
            return list(polygonize(unary_union(lines)))
    return []


# La Paloma y Cogotí no aparecen en la consulta regional (mapeo OSM irregular:
# piezas sin nombre / relation que el bbox no devuelve) — consultas dirigidas.
TARGETED = {
    "Embalse La Paloma": (-30.74, -70.94),
    "Embalse Cogotí": (-31.00, -71.08),
}


def targeted_elements() -> list:
    out = []
    for nombre, (lat, lon) in TARGETED.items():
        q = (
            f'[out:json][timeout:60];('
            f'way["natural"="water"]["water"="reservoir"](around:8000,{lat},{lon});'
            f'relation["natural"="water"]["water"="reservoir"](around:8000,{lat},{lon});'
            f");out geom tags;"
        )
        data = osm.overpass(q)
        for el in data.get("elements", []):
            el.setdefault("tags", {})
            # las piezas sin nombre alrededor del punto pertenecen al embalse buscado
            if not el["tags"].get("name"):
                el["tags"]["name"] = nombre + " *"
        out.extend(data.get("elements", []))
    return out


def main():
    data = osm.overpass(QUERY)
    elements = data.get("elements", []) + targeted_elements()
    rows = []
    for el in elements:
        polys = element_polygons(el)
        if not polys:
            continue
        nombre = el.get("tags", {}).get("name", "")
        geom = unary_union(polys)
        rows.append({"geometry": geom, "nombre": nombre})

    gdf = gpd.GeoDataFrame(rows, crs="EPSG:4326")
    # unir por nombre: un embalse mapeado en piezas (o traído por dos consultas)
    # queda como un solo polígono
    named = gdf[gdf["nombre"] != ""].dissolve(by="nombre", as_index=False)
    unnamed = gdf[gdf["nombre"] == ""]
    gdf = gpd.GeoDataFrame(pd.concat([named, unnamed]), crs="EPSG:4326").reset_index(drop=True)
    # área en UTM para filtrar tranques chicos
    gdf["area_km2"] = (gdf.to_crs("EPSG:32719").area / 1e6).round(2)
    gdf = gdf[(gdf["area_km2"] >= MIN_AREA_KM2) | (gdf["nombre"] != "")].reset_index(drop=True)

    gdf.to_file(OUT_PATH, driver="GeoJSON")
    print(f"{len(gdf)} embalses guardados en {OUT_PATH}")
    for _, r in gdf.sort_values("area_km2", ascending=False).head(12).iterrows():
        print(f"  {r['nombre'] or '(sin nombre)'} — {r['area_km2']} km²")


if __name__ == "__main__":
    main()
