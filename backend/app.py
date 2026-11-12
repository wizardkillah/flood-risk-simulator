"""
Backend local del prototipo — sirve el terreno procesado (quantized-mesh),
la cuenca, los marcadores y el frontend estático.

Correr con: uvicorn backend.app:app --reload --port 8000
"""

from pathlib import Path

import logging
import os
import threading
import time
from collections import defaultdict, deque
from typing import Literal

from fastapi import FastAPI, HTTPException, Path as PathParam, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.rainfall import fetch_rainfall_24h, fetch_forecast_7d
from backend.flood import estimate_flood, estimate_flood_at_level, MAX_DELTA_H_M
from backend.terrain_profile import get_terrain_profile

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"
TERRAIN_DIR = ROOT / "data" / "terrain_tiles"
HYDRO_DIR = ROOT / "data" / "hydro"
FLOOD_DIR = ROOT / "data" / "flood"
DATA_DIR = ROOT / "data"

log = logging.getLogger("simulador")

# Sin /docs ni /openapi.json en producción (ENABLE_DOCS=1 para desarrollo).
_docs = os.environ.get("ENABLE_DOCS") == "1"
app = FastAPI(
    title="Simulador de crecidas del Limarí — Fase 1 (prototipo cartográfico)",
    docs_url="/docs" if _docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _docs else None,
)

# ---------------------------------------------------------------------------
# Seguridad: cabeceras, HTTPS y rate limit por IP.
# CORS: no se habilita a propósito. El frontend se sirve desde el mismo origen.
# ---------------------------------------------------------------------------
FORCE_HTTPS = os.environ.get("FORCE_HTTPS") == "1"   # detrás de un proxy TLS (ALB, nginx)

CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cesium.com https://cdn.jsdelivr.net 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline' https://cesium.com https://cdn.jsdelivr.net; "
    "img-src 'self' data: blob: https:; "
    "connect-src 'self' https://cesium.com https://*.cesium.com https://*.tile.openstreetmap.org https:; "
    "worker-src 'self' blob:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}

# (máx. peticiones, ventana en segundos) por IP y grupo de rutas.
_LIMITS = {
    "ai": (4, 60.0),        # /api/ai_analysis y /api/chat: cada llamada cuesta dinero
    "heavy": (30, 60.0),    # cálculos pesados (inundación, perfil de terreno)
    "default": (240, 60.0),
}
_hits: dict[tuple[str, str], deque] = defaultdict(deque)
_hits_lock = threading.Lock()


_MAX_TRACKED = 10_000


def _prune_hits(now: float) -> None:
    """Quita las claves sin actividad dentro de la ventana más larga.

    Una IP que consulta una sola vez nunca vacía su cola (solo se vacía al
    volver a consultar), así que hay que mirar la marca de tiempo más reciente.
    Debe llamarse con _hits_lock tomado.
    """
    horizon = max(window for _, window in _LIMITS.values())
    for k in [k for k, q in _hits.items() if not q or now - q[-1] > horizon]:
        del _hits[k]


def _group(path: str) -> str:
    if path in ("/api/ai_analysis", "/api/chat"):
        return "ai"
    if path.startswith(("/api/flood", "/api/terrain_profile")):
        return "heavy"
    return "default"


def _client_ip(request: Request) -> str:
    """IP del cliente para el rate limit.

    Solo se confía en X-Forwarded-For si se declara que hay un proxy delante
    (TRUST_PROXY=1). Cada proxy AÑADE al final la IP que ve; lo que viene a la
    izquierda lo escribe el cliente y se puede falsificar. Por eso se toma la
    entrada contada desde la DERECHA: TRUSTED_PROXY_HOPS=1 con un ALB,
    2 con CloudFront + ALB. Si la cabecera trae menos entradas de las
    esperadas, se usa la IP del socket.
    """
    if os.environ.get("TRUST_PROXY") == "1":
        try:
            hops = max(1, int(os.environ.get("TRUSTED_PROXY_HOPS", "1")))
        except ValueError:
            hops = 1
        parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(parts) >= hops:
            return parts[-hops]
    return request.client.host if request.client else "unknown"


def _allowed(ip: str, group: str) -> tuple[bool, int]:
    limit, window = _LIMITS[group]
    now = time.monotonic()
    with _hits_lock:
        q = _hits[(ip, group)]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False, int(window - (now - q[0])) + 1
        q.append(now)
        if len(_hits) > _MAX_TRACKED:      # evita crecimiento sin límite
            _prune_hits(now)
    return True, 0


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    if FORCE_HTTPS and request.headers.get("x-forwarded-proto", request.url.scheme) != "https":
        return JSONResponse({"detail": "HTTPS requerido"}, status_code=426)
    if request.url.path.startswith("/api/"):
        ok, retry = _allowed(_client_ip(request), _group(request.url.path))
        if not ok:
            return JSONResponse({"detail": "Demasiadas peticiones"}, status_code=429,
                                headers={"Retry-After": str(retry), **SECURITY_HEADERS})
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    if FORCE_HTTPS:
        response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
    return response


def _upstream_error(msg: str, exc: Exception) -> HTTPException:
    """502 sin filtrar detalles internos al cliente; el detalle va al log."""
    log.warning("%s: %r", msg, exc)
    return HTTPException(502, msg)


@app.get("/terrain/layer.json")
def terrain_layer():
    path = TERRAIN_DIR / "layer.json"
    if not path.exists():
        raise HTTPException(404, "Terreno no generado aún. Corre scripts/03_process_terrain.py")
    return FileResponse(path, media_type="application/json")


@app.get("/terrain/{z}/{x}/{y}.terrain")
def terrain_tile(z: int = PathParam(..., ge=0, le=20), x: int = PathParam(..., ge=0), y: int = PathParam(..., ge=0)):
    path = TERRAIN_DIR / str(z) / str(x) / f"{y}.terrain"
    if not path.exists():
        raise HTTPException(404, "Tile no disponible")
    return FileResponse(
        path,
        media_type="application/vnd.quantized-mesh",
        headers={"Content-Encoding": "gzip"},
    )


@app.get("/api/basin")
def basin_geojson():
    path = HYDRO_DIR / "limari_basin.geojson"
    if not path.exists():
        raise HTTPException(404, "Cuenca no generada aún. Corre scripts/01_delimit_basin.py")
    return FileResponse(path, media_type="application/geo+json")


@app.get("/api/markers")
def markers():
    path = DATA_DIR / "markers.json"
    if not path.exists():
        raise HTTPException(404, "markers.json no encontrado")
    return FileResponse(path, media_type="application/json")


@app.get("/api/rainfall")
def rainfall(refresh: bool = False):
    try:
        return fetch_rainfall_24h(force=refresh)
    except Exception as exc:
        raise _upstream_error("No se pudo obtener lluvia de Open-Meteo", exc)


@app.get("/api/flood")
def flood(
    refresh: bool = False,
    delta_h: float | None = Query(None, ge=0, le=30),
    rainfall_mm: float | None = Query(None, ge=0, le=1000),
):
    """
    Sin parámetros: modo automático, nivel derivado de la lluvia real de las
    últimas 24h (llama a Open-Meteo, con caché).
    Con `rainfall_mm`: simula la inundación para esa cantidad de lluvia (mm),
    usando el mismo modelo escorrentía->HAND, sin depender de la lluvia real.
    Con `delta_h` (metros, 0-30): modo manual/slider directo sobre el nivel,
    sin pasar por lluvia — responde rápido para arrastrar el control.
    """
    try:
        if delta_h is not None:
            return estimate_flood_at_level(delta_h)
        if rainfall_mm is not None:
            result = estimate_flood(rainfall_mm)
            result["mode"] = "simulated_rainfall"
            return result
        rain = fetch_rainfall_24h(force=refresh)
        result = estimate_flood(rain["basin_average_mm"])
        result["rainfall_fetched_at"] = rain["fetched_at"]
        return result
    except FileNotFoundError:
        raise HTTPException(
            404, "HAND no generado aún. Corre scripts/05_compute_hand.py"
        )
    except Exception as exc:
        raise _upstream_error("No se pudo estimar la inundación", exc)


@app.get("/api/flood/config")
def flood_config():
    return {"max_delta_h_m": MAX_DELTA_H_M}


@app.get("/api/river")
def river_network():
    path = FLOOD_DIR / "river_network.geojson"
    if not path.exists():
        raise HTTPException(404, "Red de río no generada aún. Corre scripts/05_compute_hand.py")
    return FileResponse(path, media_type="application/geo+json")


@app.get("/api/waterways")
def waterways(full: bool = False):
    """Red hídrica real de OSM: ríos, esteros permanentes.
    Por defecto sirve la versión simplificada (~150m Douglas-Peucker,
    ~60x más liviana). Pasa ?full=true para el crudo — solo para análisis,
    NO para el visor: 17MB / 4855 tramos ahogan Cesium."""
    if not full:
        simple = HYDRO_DIR / "osm_waterways.simple.geojson"
        if simple.exists():
            return FileResponse(simple, media_type="application/geo+json")
    path = HYDRO_DIR / "osm_waterways.geojson"
    if not path.exists():
        raise HTTPException(404, "Red OSM no descargada aún. Corre scripts/08_fetch_osm_hydro.py")
    return FileResponse(path, media_type="application/geo+json")


@app.get("/api/crossings")
def crossings():
    """Badenes (ford) y pasos a nivel de OSM — puntos críticos en crecidas."""
    path = HYDRO_DIR / "osm_crossings.geojson"
    if not path.exists():
        raise HTTPException(404, "Cruces OSM no descargados aún. Corre scripts/08_fetch_osm_hydro.py")
    return FileResponse(path, media_type="application/geo+json")


@app.get("/api/situation")
def situation():
    """Situación de emergencia actual recopilada de prensa (con fuentes).
    Actualizada manualmente/por sesión de trabajo — ver 'actualizado'."""
    path = HYDRO_DIR / "situacion_actual.json"
    if not path.exists():
        raise HTTPException(404, "Sin datos de situación actual")
    return FileResponse(path, media_type="application/json")


@app.get("/api/places_risk")
def places_risk():
    """Lugares poblados OSM con clasificación de riesgo por cercanía a cauces."""
    path = HYDRO_DIR / "places_risk.geojson"
    if not path.exists():
        raise HTTPException(404, "Corre scripts/09_fetch_places_risk.py")
    return FileResponse(path, media_type="application/geo+json")


@app.get("/api/quebradas")
def quebradas(full: bool = False):
    """Quebradas intermitentes (activación rara) de la red OSM. Por defecto
    versión simplificada + filtrada (con nombre o >= 1km) — 21x más liviana."""
    if not full:
        simple = HYDRO_DIR / "quebradas_intermitentes.simple.geojson"
        if simple.exists():
            return FileResponse(simple, media_type="application/geo+json")
    path = HYDRO_DIR / "quebradas_intermitentes.geojson"
    if not path.exists():
        raise HTTPException(404, "Corre scripts/10_analyze_quebradas.py")
    return FileResponse(path, media_type="application/geo+json")


@app.get("/api/confluencias")
def confluencias():
    """Puntos donde quebradas intermitentes descargan a ríos permanentes."""
    path = HYDRO_DIR / "confluencias.geojson"
    if not path.exists():
        raise HTTPException(404, "Corre scripts/10_analyze_quebradas.py")
    return FileResponse(path, media_type="application/geo+json")


@app.get("/api/relaves")
def relaves():
    """Depósitos de relaves de SERNAGEOMIN en la Región de Coquimbo, enriquecidos
    con distancia al río más cercano y polilínea aguas abajo para animar
    partículas de derrame. Corre scripts/11_fetch_relaves.py para generar."""
    path = HYDRO_DIR / "relaves.geojson"
    if not path.exists():
        raise HTTPException(404, "Relaves no descargados aún. Corre scripts/11_fetch_relaves.py")
    return FileResponse(path, media_type="application/geo+json")


@app.get("/api/reservoirs")
def reservoirs():
    path = HYDRO_DIR / "reservoirs.geojson"
    if not path.exists():
        raise HTTPException(404, "Embalses no generados aún. Corre scripts/06_fetch_reservoirs.py")
    return FileResponse(path, media_type="application/geo+json")


@app.get("/api/subbasins")
def subbasins():
    path = HYDRO_DIR / "subbasins.geojson"
    if not path.exists():
        raise HTTPException(404, "Sub-cuencas no generadas aún. Corre scripts/07_delimit_subbasins.py")
    return FileResponse(path, media_type="application/geo+json")


@app.get("/api/wind_grid")
def wind_grid(refresh: bool = False):
    """Grilla de viento a 10m (u/v en m/s) para animar partículas en el visor."""
    from backend.wind import fetch_wind_grid
    try:
        return fetch_wind_grid(force=refresh)
    except Exception as exc:
        raise _upstream_error("No se pudo obtener el viento de Open-Meteo", exc)


@app.get("/api/forecast")
def forecast(refresh: bool = False):
    try:
        return fetch_forecast_7d(force=refresh)
    except Exception as exc:
        raise _upstream_error("No se pudo obtener el pronóstico de Open-Meteo", exc)


@app.get("/api/flood/forecast_day")
def flood_forecast_day(day: int = Query(..., ge=1, le=7)):
    """delta_h/mancha estimada para la lluvia ACUMULADA desde hoy hasta el día `day` (1-7)."""
    try:
        fc = fetch_forecast_7d()
        cumulative_mm = sum(d["mm"] for d in fc["days"][:day])
        result = estimate_flood(cumulative_mm)
        result["mode"] = "forecast_cumulative"
        result["forecast_day"] = day
        result["cumulative_rainfall_mm"] = round(cumulative_mm, 1)
        return result
    except FileNotFoundError:
        raise HTTPException(404, "HAND no generado aún. Corre scripts/05_compute_hand.py")
    except Exception as exc:
        raise _upstream_error("No se pudo estimar la inundación del pronóstico", exc)


@app.get("/api/ai_analysis")
def ai_analysis(refresh: bool = False):  # el rate limit "ai" aplica por IP
    """Análisis de riesgo generado por Claude (ver backend/ai_advisor.py).
    Cacheado 15 min — cada análisis nuevo es una llamada real al modelo."""
    from backend.ai_advisor import analyze

    try:
        return analyze(force=refresh)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
    except Exception as exc:
        raise _upstream_error("No se pudo generar el análisis IA", exc)


@app.get("/api/terrain_profile")
def terrain_profile(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    size_km: float = Query(3.0, ge=0.5, le=10),
):
    try:
        return get_terrain_profile(lat, lon, size_km)
    except Exception as exc:
        raise _upstream_error("No se pudo recortar el terreno local", exc)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=2000)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=20)


@app.post("/api/chat")
def chat_endpoint(req: ChatRequest, request: Request):
    """Conversación con Claude + tool use. Devuelve texto y acciones que el
    visor ejecuta (fly-to, mover slider, abrir corte 3D)."""
    from backend.chat import chat as chat_fn, RateLimitError

    ip = _client_ip(request)
    try:
        return chat_fn(
            req.message,
            [m.model_dump() for m in req.history],
            ip,
        )
    except RateLimitError as exc:
        raise HTTPException(429, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
    except Exception as exc:
        raise _upstream_error("Error en el chat", exc)


# Debe ir al final: cualquier ruta no capturada arriba sirve el frontend estático.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
