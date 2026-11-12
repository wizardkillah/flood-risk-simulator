"""
Descarga el catastro público de depósitos de relaves de SERNAGEOMIN filtrado
a la Región de Coquimbo, y enriquece cada relave con:
- distancia al río permanente más cercano (usando osm_waterways.geojson)
- nombre del río más cercano
- polilínea "aguas abajo" (~5km) siguiendo el LineString del río nearest,
  con dirección determinada por elevación del DEM (endpoint más bajo = aguas
  abajo). Se usa como trayectoria para las partículas de derrame.
- elevación del relave (msnm) desde el DEM Copernicus

Fuente: ArcGIS Feature Server oficial del SMA/SERNAGEOMIN
  https://ideserver.sma.gob.cl/arcgis/rest/services/IDE/Industria_y_actividades/MapServer/4
  layer "Relaves - SERNAGEOMIN" (catastro 2018, 406 puntos en Coquimbo).

IMPORTANTE: este catastro se actualiza cada 2-3 años. La versión oficial más
reciente (2024) migró a un dashboard PowerBI/ArcGIS Experience — cuando
SERNAGEOMIN vuelva a exponer feature service actualizado, cambiar URL aquí.
Los datos NO son en tiempo real y NO reemplazan las notificaciones DS 248
del propio operador minero.

Salida:
  data/hydro/relaves.geojson  — puntos con propiedades enriquecidas
"""

import json
import re
from pathlib import Path

import requests
import rasterio
from shapely.geometry import shape, Point, LineString
from shapely.ops import nearest_points, transform as shp_transform
from shapely.strtree import STRtree
import pyproj

ROOT = Path(__file__).resolve().parent.parent
DEM_PATH = ROOT / "data" / "dem" / "limari_cop30.tif"
WATERWAYS_PATH = ROOT / "data" / "hydro" / "osm_waterways.geojson"
OUT_PATH = ROOT / "data" / "hydro" / "relaves.geojson"

# Bbox de la Región de Coquimbo (coincide con el resto del pipeline)
BBOX = {"west": -71.80, "east": -69.75, "south": -32.35, "north": -29.0}

ARCGIS_URL = (
    "https://ideserver.sma.gob.cl/arcgis/rest/services/"
    "IDE/Industria_y_actividades/MapServer/4/query"
)

# Distancia máxima aguas abajo a trazar por relave (metros). Ajustar según
# alcance visual deseado en el visor.
DOWNSTREAM_MAX_M = 8000

# Umbral para considerar el relave "río-adyacente" en la evaluación de riesgo
# (metros). Los que estén más lejos también se muestran pero como bajo riesgo
# geométrico.
NEAR_RIVER_M = 500

# Proyección local (métrica) para calcular distancias en metros con precisión
# razonable en Coquimbo (~30°S). UTM Zone 19S = EPSG:32719.
_LATLON = pyproj.CRS("EPSG:4326")
_UTM19S = pyproj.CRS("EPSG:32719")
_TO_UTM = pyproj.Transformer.from_crs(_LATLON, _UTM19S, always_xy=True).transform
_TO_LL = pyproj.Transformer.from_crs(_UTM19S, _LATLON, always_xy=True).transform


def fetch_arcgis() -> list[dict]:
    """Consulta el feature service y devuelve features (Esri JSON)."""
    envelope = {
        "xmin": BBOX["west"], "ymin": BBOX["south"],
        "xmax": BBOX["east"], "ymax": BBOX["north"],
        "spatialReference": {"wkid": 4326},
    }
    params = {
        "where": "1=1",
        "geometry": json.dumps(envelope),
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "outSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "true",
        "f": "json",
    }
    r = requests.get(ARCGIS_URL, params=params, timeout=60, verify=False)
    r.raise_for_status()
    data = r.json()
    if data.get("exceededTransferLimit"):
        print("[AVISO] exceededTransferLimit=true — hay más features. "
              "Implementar paginación con resultOffset/resultRecordCount.")
    return data.get("features", [])


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(html: str) -> str:
    return _WS_RE.sub(" ", _HTML_TAG_RE.sub(" ", html or "")).strip()


def parse_popup(popup_html: str) -> dict:
    """Extrae campos útiles del HTML del PopupInfo. Los formatos varían por
    región/año; capturamos lo que aparezca (tipo, empresa, estado, tonelaje)."""
    text = _strip_html(popup_html)
    out: dict = {}
    patterns = {
        "empresa":  r"Empresa\s*:?\s*([^:]+?)(?=\s+(?:Faena|Nombre|Región|Tipo|Estado|Tonelaje|$))",
        "faena":    r"Faena\s*:?\s*([^:]+?)(?=\s+(?:Empresa|Nombre|Región|Tipo|Estado|Tonelaje|$))",
        "tipo":     r"Tipo\s+de\s+dep(?:o|ó)sito\s*:?\s*([^:]+?)(?=\s+(?:Estado|Tonelaje|Método|$))",
        "estado":   r"Estado\s+actual\s*:?\s*([^:]+?)(?=\s+(?:Tipo|Tonelaje|Método|$))",
        "tonelaje": r"Tonelaje\s+autorizado\s*(?:\([^)]+\))?\s*:?\s*([\d.,]+)",
        "comuna":   r"Comuna\s*:?\s*([^:]+?)(?=\s+(?:Provincia|Región|Tipo|$))",
    }
    for key, pat in patterns.items():
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            out[key] = m.group(1).strip()
    return out


def sample_dem_elev(dem, lon: float, lat: float) -> float | None:
    """Elevación (msnm) del DEM en un punto. Devuelve None si cae fuera."""
    try:
        vals = list(dem.sample([(lon, lat)]))
        v = float(vals[0][0])
        if v < -400 or v > 8000:
            return None
        return v
    except Exception:
        return None


def load_waterways() -> list[dict]:
    """Carga solo cauces permanentes (no intermitentes) con geometría LineString.
    Pre-proyecta cada línea a UTM19S y arma un STRtree para búsqueda O(log n)."""
    gj = json.loads(WATERWAYS_PATH.read_text(encoding="utf-8"))
    out = []
    for f in gj.get("features", []):
        props = f.get("properties", {})
        if props.get("intermittent") == "yes":
            continue
        geom = f.get("geometry", {})
        if geom.get("type") != "LineString":
            continue
        try:
            line_ll = LineString(geom["coordinates"])
            coords_utm = [_TO_UTM(x, y) for x, y in line_ll.coords]
            line_utm = LineString(coords_utm)
        except Exception:
            continue
        out.append({
            "geom_ll": line_ll,       # LineString EPSG:4326
            "geom_utm": line_utm,     # LineString EPSG:32719 (metros)
            "name": props.get("nombre") or "",
        })
    return out


def build_river_index(rivers: list[dict]) -> STRtree:
    """Índice espacial sobre las geometrías UTM. STRtree preserva orden de
    entrada, así que .query() devuelve índices que mapean 1:1 al `rivers` list."""
    return STRtree([r["geom_utm"] for r in rivers])


def find_nearest_river(pt_ll: Point, rivers: list[dict], tree: STRtree) -> tuple[dict | None, float, Point | None, int]:
    """Devuelve (river_dict, dist_m, nearest_point_ll, nearest_vertex_idx).
    Usa STRtree.query_nearest para O(log n) en promedio (~10ms/relave)."""
    pt_utm = Point(_TO_UTM(pt_ll.x, pt_ll.y))
    # query_nearest devuelve índice(s) del/los más cercanos. Pedimos 1.
    idxs = tree.query_nearest(pt_utm, return_distance=False, all_matches=False)
    if len(idxs) == 0:
        return None, float("inf"), None, -1
    idx = int(idxs[0])
    r = rivers[idx]
    line_utm = r["geom_utm"]
    dist_m = line_utm.distance(pt_utm)
    np_utm = nearest_points(line_utm, pt_utm)[0]
    np_ll = Point(*_TO_LL(np_utm.x, np_utm.y))
    coords = list(line_utm.coords)
    # vértice de la línea más cercano al nearest_point
    vd = [(c[0] - np_utm.x) ** 2 + (c[1] - np_utm.y) ** 2 for c in coords]
    best_vertex_idx = int(min(range(len(vd)), key=lambda i: vd[i]))
    return r, dist_m, np_ll, best_vertex_idx


def downstream_path(river: dict, vertex_idx: int, dem, max_m: float) -> list[list[float]]:
    """Extrae hasta max_m metros aguas abajo del vértice inicial, siguiendo el
    LineString. Dirección: comparamos elevación del primer y último vértice —
    la más baja indica el mouth. Si el DEM no muestrea (fuera del bbox), asumimos
    forward (OSM suele digitalizar upstream → downstream, imperfecto)."""
    coords = list(river["geom_ll"].coords)  # [(lon, lat), ...]
    if len(coords) < 2:
        return []
    e_first = sample_dem_elev(dem, coords[0][0], coords[0][1])
    e_last = sample_dem_elev(dem, coords[-1][0], coords[-1][1])
    forward = True
    if e_first is not None and e_last is not None:
        forward = e_last < e_first
    seq = coords[vertex_idx:] if forward else list(reversed(coords[:vertex_idx + 1]))
    if len(seq) < 2:
        return []

    # cortar por longitud acumulada en metros (proyectamos on the fly)
    acc = 0.0
    out = [[float(seq[0][0]), float(seq[0][1])]]
    prev_utm = _TO_UTM(seq[0][0], seq[0][1])
    for lon, lat in seq[1:]:
        cur_utm = _TO_UTM(lon, lat)
        d = ((cur_utm[0] - prev_utm[0]) ** 2 + (cur_utm[1] - prev_utm[1]) ** 2) ** 0.5
        acc += d
        out.append([float(lon), float(lat)])
        if acc >= max_m:
            break
        prev_utm = cur_utm
    return out


def main():
    print(f"Descargando catastro SERNAGEOMIN — bbox {BBOX} …", flush=True)
    esri_features = fetch_arcgis()
    print(f"  {len(esri_features)} relaves recibidos.", flush=True)

    print("Cargando red hídrica OSM (permanentes) …", flush=True)
    rivers = load_waterways()
    print(f"  {len(rivers)} cauces permanentes.", flush=True)
    print("Construyendo índice espacial (STRtree) …", flush=True)
    tree = build_river_index(rivers)

    print("Abriendo DEM …", flush=True)
    dem = rasterio.open(DEM_PATH) if DEM_PATH.exists() else None
    if dem is None:
        print(f"  [AVISO] No hay DEM en {DEM_PATH}. Sin elevaciones ni dirección aguas abajo asegurada.", flush=True)

    out_features: list[dict] = []
    for i, ef in enumerate(esri_features, 1):
        attrs = ef.get("attributes", {})
        geom = ef.get("geometry", {})
        lon, lat = geom.get("x"), geom.get("y")
        if lon is None or lat is None:
            continue
        pt = Point(lon, lat)
        popup = parse_popup(attrs.get("PopupInfo", ""))

        river, dist_m, np_ll, v_idx = find_nearest_river(pt, rivers, tree)
        river_name = river["name"] if river else ""
        dpath: list[list[float]] = []
        if river and dem is not None:
            dpath = downstream_path(river, v_idx, dem, DOWNSTREAM_MAX_M)
        elev = sample_dem_elev(dem, lon, lat) if dem is not None else None

        # riesgo geométrico simple (proxy — no reemplaza análisis hidrológico)
        if dist_m < NEAR_RIVER_M:
            geom_risk = "alto"
        elif dist_m < 2 * NEAR_RIVER_M:
            geom_risk = "medio"
        else:
            geom_risk = "bajo"

        props = {
            "nombre": (attrs.get("Name") or "").strip() or "Relave sin nombre",
            "elev_m": round(elev, 1) if elev is not None else None,
            "rio_cercano": river_name or None,
            "dist_rio_m": round(dist_m, 1),
            "riesgo_geometrico": geom_risk,
            "downstream_path": dpath,   # [[lon,lat],...] hasta ~DOWNSTREAM_MAX_M m
            "sernageomin_oid": attrs.get("OBJECTID"),
            **popup,
        }
        out_features.append({
            "type": "Feature",
            "properties": props,
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
        })

        if i % 50 == 0:
            print(f"  procesados {i}/{len(esri_features)}", flush=True)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": out_features,
        "_source": (
            "SERNAGEOMIN vía ArcGIS Feature Server SMA "
            "(Industria_y_actividades/MapServer/4, catastro 2018). "
            "Enriquecido con osm_waterways.geojson y DEM Copernicus GLO-30."
        ),
        "_bbox": BBOX,
        "_downstream_max_m": DOWNSTREAM_MAX_M,
        "_disclaimer": (
            "Coordenadas y atributos del catastro público oficial. NO reemplaza "
            "notificaciones DS 248 del operador. NO usar para decisiones de "
            "emergencia — prototipo educativo."
        ),
    }, ensure_ascii=False), encoding="utf-8")
    print(f"OK — {len(out_features)} relaves escritos en {OUT_PATH}")


if __name__ == "__main__":
    # Silenciar warning de InsecureRequestWarning por verify=False (SERNAGEOMIN/SMA
    # a veces tienen cadena de certs incompleta hacia ciertos hosts)
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    main()
