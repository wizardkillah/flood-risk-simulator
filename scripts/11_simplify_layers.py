"""
Simplificación de geometrías pesadas para el visor.

Los GeoJSON crudos de OSM regional son enormes:
- osm_waterways.geojson: 17 MB, 4855 tramos, ~1M vértices
- quebradas_intermitentes.geojson: 12 MB, 3695 tramos

Cesium se ahoga al cargarlos porque cada tramo es una Entity independiente y
cada vértice se convierte a Cartesian3 + posición GPU en cada frame. Además
"waterways" trae también los intermitentes y canales, que ya viven en otras
capas — se sirve tres veces la misma geometría.

Este script produce versiones optimizadas para el visor:

- osm_waterways.simple.geojson: SOLO ríos y esteros permanentes (sin
  intermitentes ni canales), simplificados con Douglas-Peucker a ~150m.
  Baja de 17MB a ~1-2MB.
- quebradas_intermitentes.simple.geojson: solo quebradas con nombre O
  suficientemente largas (>1km), simplificadas ~200m. Baja de 12MB a ~1-2MB.

Los archivos originales se mantienen — son la fuente de verdad para análisis
(scripts/10, scripts/09), estas versiones son solo para render.
"""

from pathlib import Path

import geopandas as gpd

ROOT = Path(__file__).resolve().parent.parent
HYDRO_DIR = ROOT / "data" / "hydro"

UTM = "EPSG:32719"
TOL_PERMANENTES_M = 150   # ríos: relieve suave, 150m es imperceptible en el visor regional
TOL_QUEBRADAS_M = 200     # quebradas: aún más agresivo (son secundarias)
MIN_LEN_QUEBRADA_KM = 1.0  # bajo esto, solo si tiene nombre


def simplify_waterways():
    src = HYDRO_DIR / "osm_waterways.geojson"
    dst = HYDRO_DIR / "osm_waterways.simple.geojson"
    gdf = gpd.read_file(src)
    print(f"waterways crudo: {len(gdf)} tramos")

    # Solo cauces naturales permanentes — intermitentes y canales viven en
    # su propia capa (o no se muestran para nada, como los canales de riego)
    permanentes = gdf[(gdf["tipo"].isin(["river", "stream"])) & (~gdf["intermitente"])].copy()
    print(f"  permanentes (river+stream, no intermitentes): {len(permanentes)}")

    # simplificar en metros (UTM)
    utm = permanentes.to_crs(UTM)
    utm["geometry"] = utm.geometry.simplify(TOL_PERMANENTES_M, preserve_topology=True)
    utm = utm[~utm.geometry.is_empty]
    simplificado = utm.to_crs("EPSG:4326")

    simplificado.to_file(dst, driver="GeoJSON")
    size_kb = dst.stat().st_size / 1024
    print(f"  guardado: {dst.name} ({size_kb:.0f} KB, era {src.stat().st_size/1024:.0f} KB)")


def simplify_quebradas():
    src = HYDRO_DIR / "quebradas_intermitentes.geojson"
    dst = HYDRO_DIR / "quebradas_intermitentes.simple.geojson"
    gdf = gpd.read_file(src)
    print(f"quebradas crudo: {len(gdf)} tramos")

    # medir longitud en UTM
    utm = gdf.to_crs(UTM)
    utm["_len_km"] = utm.geometry.length / 1000
    # Filtro: con nombre O >= 1 km. Las quebradas cortas sin nombre son ruido
    # visual (excepto en zoom cercano, pero el visor regional está lejos).
    con_nombre = utm["nombre"].astype(str).str.strip() != ""
    keep = utm[con_nombre | (utm["_len_km"] >= MIN_LEN_QUEBRADA_KM)].copy()
    print(f"  con nombre o >= {MIN_LEN_QUEBRADA_KM}km: {len(keep)}")

    keep["geometry"] = keep.geometry.simplify(TOL_QUEBRADAS_M, preserve_topology=True)
    keep = keep[~keep.geometry.is_empty]
    keep = keep.drop(columns=["_len_km"])
    simplificado = keep.to_crs("EPSG:4326")

    simplificado.to_file(dst, driver="GeoJSON")
    size_kb = dst.stat().st_size / 1024
    print(f"  guardado: {dst.name} ({size_kb:.0f} KB, era {src.stat().st_size/1024:.0f} KB)")


def main():
    simplify_waterways()
    print()
    simplify_quebradas()


if __name__ == "__main__":
    main()
