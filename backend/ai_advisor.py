"""
Asesor de alertas de crecida con Claude (API de Anthropic).

Junta en un solo contexto: la lluvia real de las últimas 24h, el pronóstico
a 7 días, la estimación HAND de inundación actual, los registros históricos
de lluvia de la zona (backend/rain_history.py) y el inventario de puntos
críticos (badenes, pasos a nivel, embalses, red de ríos con nombre), y le
pide a Claude un análisis estructurado: nivel de alerta, análisis climático,
comparación histórica, zonas en riesgo, recomendaciones y criterio de
evacuación preventiva.

Requiere ANTHROPIC_API_KEY (en .env o en el entorno). El resultado se cachea
15 minutos — cada análisis es una llamada real al modelo y tiene costo.

IMPORTANTE: igual que el resto del prototipo, esto NO reemplaza a la DGA ni
a SENAPRED. El propio prompt obliga al modelo a decirlo.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from backend.flood import estimate_flood
from backend.rain_history import get_history_stats
from backend.rainfall import fetch_forecast_7d, fetch_rainfall_24h

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

HYDRO_DIR = ROOT / "data" / "hydro"

MODEL = os.environ.get("AI_ADVISOR_MODEL", "claude-opus-4-8")
CACHE_TTL_S = 15 * 60
# Piso entre llamadas reales al modelo aunque venga refresh=true: cada análisis
# cuesta dinero — esto acota el peor caso (clic compulsivo / bucle) a 1/min.
FORCE_MIN_INTERVAL_S = 60

_cache: dict = {"at": 0.0, "result": None}
# Serializa las llamadas: dos requests simultáneas (doble clic, dos pestañas)
# no deben disparar dos llamadas pagadas — la segunda espera y recibe el caché.
_lock = threading.Lock()

SYSTEM_PROMPT = """Eres el asesor hidrometeorológico de un prototipo de simulador de \
crecidas de la Región de Coquimbo, Chile (cuencas de Elqui, Limarí y Choapa más las \
quebradas costeras de La Serena/Coquimbo). Tu trabajo es analizar los datos que te \
entrega el sistema (lluvia actual, pronóstico a 7 días, estimación de inundación por \
modelo HAND, estadísticas históricas de lluvia, situación de emergencia reportada por \
prensa, comunidades cercanas a cauces y el inventario de puntos críticos) y producir \
una evaluación de riesgo de inundación clara y accionable en español chileno.

Si el contexto incluye "situacion_reportada" (emergencia en curso según prensa), \
dale prioridad: la realidad reportada manda sobre el modelo, y tu análisis debe \
identificar los puntos más afectados y las comunidades que aún corren peligro.

Reglas:
- Basa el análisis SOLO en los datos entregados y en tu conocimiento general de la \
hidrología de cuencas semiáridas chilenas (crecidas rápidas, quebradas activadas por \
lluvia intensa, suelos con poca infiltración, aporte nival en cabecera).
- Compara siempre la lluvia actual y pronosticada contra los percentiles históricos \
entregados (p90/p95/p99 y días récord) para calibrar el nivel de alerta.
- Nivel de alerta: "verde" (sin riesgo apreciable), "amarilla" (vigilancia, lluvia \
significativa posible), "naranja" (riesgo real de crecida, preparar medidas), "roja" \
(crecida probable o en curso, acción inmediata). Sé conservador: no exageres con poca \
lluvia, no minimices con lluvia sobre p95.
- Zonas en riesgo: usa los lugares reales del contexto (Ovalle, los ríos/esteros/quebradas \
con nombre, los badenes y sectores con embalses). Los badenes y pasos a nivel son lo \
primero que se corta — menciónalos cuando el nivel sea amarilla o superior.
- Recomendaciones: concretas y ordenadas por prioridad (qué monitorear, qué caminos \
evitar, cuándo considerar evacuación preventiva de zonas bajas ribereñas).
- Evacuación preventiva: indica explícitamente si amerita o no, con el criterio usado.
- Sé honesto con la incertidumbre: el modelo de inundación es una estimación no \
calibrada, la lluvia viene de modelos meteorológicos (no estaciones), y el pronóstico \
pierde confiabilidad después del día 3-4.
- SIEMPRE deja claro que esto es un prototipo educativo y que las decisiones reales \
deben seguir los canales oficiales (DGA, SENAPRED, municipio)."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "nivel_alerta": {
            "type": "string",
            "enum": ["verde", "amarilla", "naranja", "roja"],
            "description": "Nivel de alerta de inundación para la cuenca",
        },
        "titulo": {"type": "string", "description": "Titular corto del estado actual (máx ~10 palabras)"},
        "resumen": {"type": "string", "description": "Resumen ejecutivo en 2-3 frases"},
        "analisis_climatico": {
            "type": "string",
            "description": "Análisis del clima actual y del pronóstico a 7 días para la cuenca",
        },
        "comparacion_historica": {
            "type": "string",
            "description": "Cómo se compara la lluvia actual/pronosticada con los registros históricos (percentiles, días récord)",
        },
        "zonas_en_riesgo": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "lugar": {"type": "string"},
                    "motivo": {"type": "string"},
                },
                "required": ["lugar", "motivo"],
                "additionalProperties": False,
            },
            "description": "Lugares concretos con riesgo de inundación o corte, con su motivo",
        },
        "recomendaciones": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Acciones concretas ordenadas por prioridad",
        },
        "evacuacion_preventiva": {
            "type": "string",
            "description": "Si amerita o no evacuación preventiva, de qué zonas, y con qué criterio",
        },
        "disclaimer": {"type": "string", "description": "Recordatorio de que es un prototipo no oficial"},
    },
    "required": [
        "nivel_alerta",
        "titulo",
        "resumen",
        "analisis_climatico",
        "comparacion_historica",
        "zonas_en_riesgo",
        "recomendaciones",
        "evacuacion_preventiva",
        "disclaimer",
    ],
    "additionalProperties": False,
}


def _count_geojson(path: Path, prop: str | None = None) -> dict:
    """Cuenta features de un geojson, opcionalmente agrupando por una propiedad."""
    if not path.exists():
        return {}
    gj = json.loads(path.read_text(encoding="utf-8"))
    feats = gj.get("features", [])
    if prop is None:
        return {"total": len(feats)}
    out: dict = {}
    for f in feats:
        key = f.get("properties", {}).get(prop, "") or "sin_dato"
        out[key] = out.get(key, 0) + 1
    return out


def _named_waterways(limit: int = 25) -> list[str]:
    path = HYDRO_DIR / "osm_waterways.geojson"
    if not path.exists():
        return []
    gj = json.loads(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for f in gj.get("features", []):
        n = f.get("properties", {}).get("nombre", "")
        if n and n not in names:
            names.append(n)
    # ríos primero (suelen empezar con "Río"), después esteros/quebradas
    names.sort(key=lambda n: (not n.lower().startswith("r"), n))
    return names[:limit]


def _build_context() -> dict:
    rain = fetch_rainfall_24h()
    forecast = fetch_forecast_7d()
    flood = estimate_flood(rain["basin_average_mm"])
    flood_ctx = {k: v for k, v in flood.items() if k != "geojson"}

    cumulative = 0.0
    forecast_days = []
    for d in forecast.get("days", []):
        cumulative += d["mm"]
        forecast_days.append({"fecha": d["date"], "mm_dia": d["mm"], "mm_acumulado": round(cumulative, 1)})

    reservoirs = []
    res_path = HYDRO_DIR / "reservoirs.geojson"
    if res_path.exists():
        gj = json.loads(res_path.read_text(encoding="utf-8"))
        reservoirs = [f.get("properties", {}).get("nombre", "?") for f in gj.get("features", [])]

    # situación de emergencia reportada por prensa (si existe y es reciente)
    situacion = None
    sit_path = HYDRO_DIR / "situacion_actual.json"
    if sit_path.exists():
        situacion = json.loads(sit_path.read_text(encoding="utf-8"))
        # al modelo no le sirven las URLs — resumir puntos sin fuente
        situacion["puntos_afectados"] = [
            {k: v for k, v in p.items() if k not in ("fuente", "coordenada_aproximada")}
            for p in situacion.get("puntos_afectados", [])
        ]

    # comunidades cercanas a cauces (proxy geométrico de riesgo)
    comunidades_alto = []
    places_path = HYDRO_DIR / "places_risk.geojson"
    if places_path.exists():
        gj = json.loads(places_path.read_text(encoding="utf-8"))
        alto = [f["properties"] for f in gj["features"] if f["properties"].get("riesgo") == "alto"]
        alto.sort(key=lambda p: -(p.get("poblacion") or 0))
        comunidades_alto = [
            {
                "nombre": p["nombre"],
                "tipo": p["tipo"],
                "poblacion": p.get("poblacion"),
                "dist_rio_m": p.get("dist_rio_m"),
                "dist_quebrada_m": p.get("dist_quebrada_m"),
            }
            for p in alto[:40]
        ]

    quebradas_stats = {}
    queb_path = HYDRO_DIR / "quebradas_intermitentes.geojson"
    conf_path = HYDRO_DIR / "confluencias.geojson"
    if queb_path.exists():
        quebradas_stats["quebradas_intermitentes"] = _count_geojson(queb_path)
    if conf_path.exists():
        quebradas_stats["confluencias_quebrada_a_rio"] = _count_geojson(conf_path)
        quebradas_stats["nota"] = (
            "Quebradas intermitentes = cauces secos casi siempre, se activan solo con "
            "lluvia intensa (las que sorprenden a la población). Las confluencias son "
            "los puntos donde inyectan caudal y sedimento al río receptor."
        )

    return {
        "instante_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ambito": "Región de Coquimbo completa (cuencas Elqui, Limarí y Choapa + costa)",
        "situacion_reportada": situacion,
        "lluvia_ultimas_24h": {
            "promedio_regional_mm": rain["basin_average_mm"],
            "fuente": "Open-Meteo (modelo meteorológico, no estaciones), grilla de 64 puntos",
        },
        "pronostico_7_dias": forecast_days,
        "estimacion_inundacion_actual": {
            **flood_ctx,
            "metodo": "HAND + balance de volumen, coeficiente de escorrentía fijo 0.3 — NO calibrado",
        },
        "historico_lluvia": get_history_stats(),
        "comunidades_riesgo_alto_por_cercania_a_cauce": comunidades_alto,
        "quebradas": quebradas_stats,
        "inventario_region": {
            "ciudades_principales": "La Serena-Coquimbo (conurbación ~500.000 hab), Ovalle (~111.000), Illapel, Vicuña, Salamanca, Combarbalá, Monte Patria",
            "embalses": reservoirs,
            "cursos_de_agua_con_nombre": _named_waterways(),
            "badenes_y_pasos_a_nivel": _count_geojson(HYDRO_DIR / "osm_crossings.geojson", "tipo"),
            "red_hidrica_por_tipo": _count_geojson(HYDRO_DIR / "osm_waterways.geojson", "tipo"),
            "nota": "Badenes = cruces de camino por el cauce, lo primero que se corta en crecida",
        },
    }


def analyze(force: bool = False) -> dict:
    """Genera (o devuelve cacheado) el análisis de riesgo con Claude."""

    def cached_ok(now: float) -> bool:
        if not _cache["result"]:
            return False
        age = now - _cache["at"]
        if age < FORCE_MIN_INTERVAL_S:
            return True  # ni siquiera force salta el piso de 60s
        return not force and age < CACHE_TTL_S

    if cached_ok(time.time()):
        return _cache["result"]

    with _lock:
        # doble chequeo: si otra request generó el análisis mientras esperábamos
        # el lock, devolvemos ese resultado en vez de pagar otra llamada
        now = time.time()
        if cached_ok(now):
            return _cache["result"]
        return _analyze_locked(now)


def _analyze_locked(now: float) -> dict:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "Falta ANTHROPIC_API_KEY. Agrega tu clave de https://console.anthropic.com "
            "al archivo .env (ANTHROPIC_API_KEY=sk-ant-...) y reinicia el servidor."
        )

    context = _build_context()
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        messages=[
            {
                "role": "user",
                "content": (
                    "Datos actuales del sistema de monitoreo de la cuenca del Limarí. "
                    "Genera la evaluación de riesgo:\n\n"
                    + json.dumps(context, ensure_ascii=False, indent=2)
                ),
            }
        ],
    )

    if response.stop_reason == "refusal":
        raise RuntimeError("El modelo declinó generar el análisis (refusal). Reintenta.")

    text = next(b.text for b in response.content if b.type == "text")
    analysis = json.loads(text)

    result = {
        "analysis": analysis,
        "model": response.model,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "context_summary": {
            "lluvia_24h_mm": context["lluvia_ultimas_24h"]["promedio_regional_mm"],
            "pronostico_acumulado_7d_mm": context["pronostico_7_dias"][-1]["mm_acumulado"]
            if context["pronostico_7_dias"]
            else 0,
            "area_inundable_km2": context["estimacion_inundacion_actual"].get("flooded_area_km2"),
        },
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
    }
    _cache["at"] = now
    _cache["result"] = result
    return result
