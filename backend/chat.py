"""
Chatbot conversacional del simulador — Claude con tool use.

El modelo recibe la pregunta del usuario y decide qué herramientas invocar:
- simular_lluvia(mm), simular_nivel(delta_h_m), pronostico_dia(dia)
- buscar_lugar(nombre) — resuelve nombres (Ovalle, Río Hurtado, La Paloma, etc.)
  a coordenadas revisando markers.json, places_risk, embalses y OSM waterways
- ver_terreno_en(lat, lon) — muestra el corte 3D local en ese punto
- situacion_actual(), lluvia_actual(), pronostico_7d()

La respuesta incluye texto + una lista de `frontend_actions` que el visor
ejecuta (volar la cámara, actualizar el slider, abrir el modal de corte).

Igual que ai_advisor: requiere ANTHROPIC_API_KEY, serializa llamadas para no
duplicar costo por doble-clic, y aplica un piso de tasa por-IP para acotar el
peor caso (bucle o abuso). NO cachea — cada pregunta es distinta.
"""

import json
import os
import re
import threading
import time
import unicodedata
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

from backend.flood import estimate_flood, estimate_flood_at_level
from backend.rainfall import fetch_forecast_7d, fetch_rainfall_24h

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
HYDRO_DIR = DATA_DIR / "hydro"

MODEL = os.environ.get("CHAT_MODEL", "claude-opus-4-8")
MAX_TOOL_ITERATIONS = 6
MAX_HISTORY_TURNS = 20  # pares user/assistant recientes que aceptamos
MAX_MESSAGE_CHARS = 2000

# Rate limit por IP: 8 preguntas / 60s. Cada pregunta puede consumir
# múltiples llamadas al modelo (tool loop), así que es agresivo a propósito.
RATE_LIMIT_MAX = 8
RATE_LIMIT_WINDOW_S = 60.0
_rate_state: dict[str, deque] = defaultdict(deque)
_rate_lock = threading.Lock()

# Serializa por-IP: dos preguntas simultáneas de la misma pestaña (doble
# envío) no gatillan dos loops pagados en paralelo.
_ip_locks: dict[str, threading.Lock] = {}
_MAX_TRACKED = 10_000
# Tiempo máximo de una llamada al modelo: sin esto una conexión colgada retiene
# el hilo y el candado de esa IP durante los 10 min por defecto del SDK.
CLIENT_TIMEOUT_S = 60.0


def _lock_for(ip: str) -> threading.Lock:
    with _rate_lock:
        lock = _ip_locks.get(ip)
        if lock is None:
            if len(_ip_locks) > _MAX_TRACKED:      # solo se descartan los candados libres
                for k in [k for k, v in _ip_locks.items() if not v.locked()]:
                    del _ip_locks[k]
            lock = _ip_locks[ip] = threading.Lock()
        return lock


SYSTEM_PROMPT = """Eres el asistente conversacional de un prototipo de simulador de \
crecidas de la Región de Coquimbo, Chile (cuencas de Elqui, Limarí y Choapa más \
las quebradas costeras). Ayudas al usuario a explorar escenarios sin que tenga que \
mover controles a mano: cuando pida "simula 80mm en Ovalle" o "qué pasa si llueve \
lo mismo que el temporal de 2015 en el Río Hurtado", usa las herramientas.

Herramientas disponibles:
- buscar_lugar: resuelve nombres a coordenadas (ciudades, ríos, esteros, embalses, \
  localidades). Úsalo siempre que el usuario nombre un lugar y necesites coordenadas \
  para volar la cámara o mostrar un corte.
- simular_lluvia(mm): corre el modelo HAND para esa lluvia acumulada 24h y devuelve \
  área inundada + nivel resultante.
- simular_nivel(delta_h_m): mismo modelo pero fijando directamente el alza del cauce.
- pronostico_dia(dia 1..7): simula la crecida acumulando la lluvia pronosticada \
  desde hoy hasta ese día.
- ver_terreno_en(lat, lon): abre en el visor un corte 3D del relieve real en ese \
  punto (útil después de buscar_lugar para "ver qué pasa cerca del pueblo X").
- situacion_actual, lluvia_actual, pronostico_7d: datos de contexto sin simular.

Reglas:
- Responde en español chileno, conciso y accionable. Nada de "¡Excelente pregunta!".
- Si el usuario da una cifra ambigua ("una lluvia fuerte"), pídele un rango o \
  propón uno referenciado (p.ej. "unos 50mm/24h ≈ p95 histórico regional") antes \
  de simular.
- Reporta siempre las cifras clave que devolvió la herramienta (mm, delta_h, \
  km² inundables, % de la cuenca) y contextualiza brevemente.
- Cuando resuelvas un lugar y hagas una simulación regional, incluye una nota \
  clara: el modelo HAND es de cuenca completa, no local — la mancha resultante \
  cubre TODA la zona baja de la cuenca, no solo cerca del punto consultado.
- Sé honesto con incertidumbre: modelo no calibrado, lluvia de Open-Meteo (no \
  estaciones), pronóstico pierde confianza tras el día 3-4.
- Cierra recordando que es un prototipo educativo — decisiones reales van por \
  DGA/SENAPRED/municipio."""


TOOLS = [
    {
        "name": "buscar_lugar",
        "description": (
            "Resuelve el nombre de un lugar (ciudad, localidad, río, estero, "
            "quebrada, embalse) a coordenadas lat/lon aproximadas revisando "
            "los datasets del simulador (markers, places_risk, OSM waterways, "
            "embalses). Devuelve hasta 5 candidatos ordenados por relevancia."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Texto a buscar, p.ej. 'Ovalle', 'río Hurtado', 'La Paloma'"},
            },
            "required": ["nombre"],
        },
    },
    {
        "name": "simular_lluvia",
        "description": (
            "Corre el modelo HAND para una lluvia acumulada de 24h (mm) sobre "
            "toda la cuenca del Limarí. Devuelve área inundable, % de la "
            "cuenca y alza de nivel resultante. Rango razonable: 0-300 mm."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mm": {"type": "number", "minimum": 0, "maximum": 300, "description": "Lluvia acumulada 24h en mm"},
            },
            "required": ["mm"],
        },
    },
    {
        "name": "simular_nivel",
        "description": (
            "Fija directamente el alza del cauce (delta_h en metros, 0-25) y "
            "devuelve la mancha de inundación resultante. Útil cuando el "
            "usuario piensa en términos de 'y si el río sube 3 metros'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "delta_h_m": {"type": "number", "minimum": 0, "maximum": 25},
            },
            "required": ["delta_h_m"],
        },
    },
    {
        "name": "pronostico_dia",
        "description": (
            "Simula la crecida acumulando la lluvia pronosticada desde hoy "
            "hasta el día `dia` (1 a 7)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"dia": {"type": "integer", "minimum": 1, "maximum": 7}},
            "required": ["dia"],
        },
    },
    {
        "name": "ver_terreno_en",
        "description": (
            "Abre en el visor 3D un corte del relieve real en el punto dado, "
            "con simulador local de nivel de agua. Úsalo cuando el usuario "
            "quiera ver 'cómo se ve el terreno' en un lugar específico."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "nombre": {"type": "string", "description": "Etiqueta del punto (opcional)"},
            },
            "required": ["lat", "lon"],
        },
    },
    {
        "name": "evaluar_relaves",
        "description": (
            "Evalúa qué depósitos de relaves de SERNAGEOMIN están 'en riesgo' "
            "o 'vertiendo' dados un delta_h del cauce y/o una lluvia acumulada "
            "24h. Devuelve conteos y los 10 relaves críticos más cercanos al "
            "río, con empresa/comuna. Úsalo cuando el usuario pregunte '¿qué "
            "pasa con los relaves si...?' o quiera identificar sitios en peligro."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "delta_h_m": {"type": "number", "minimum": 0, "maximum": 25,
                              "description": "Alza del cauce en metros (opcional)"},
                "rainfall_mm": {"type": "number", "minimum": 0, "maximum": 300,
                                "description": "Lluvia acumulada 24h en mm (opcional)"},
            },
        },
    },
    {
        "name": "situacion_actual",
        "description": "Devuelve la situación de emergencia actual reportada por prensa (si existe).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "lluvia_actual",
        "description": "Lluvia promedio de la cuenca en las últimas 24h (Open-Meteo).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "pronostico_7d",
        "description": "Pronóstico diario de lluvia a 7 días con acumulado.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s.strip().lower())


_places_cache: dict[str, Any] = {"at": 0.0, "items": None}


def _load_places() -> list[dict]:
    """Índice unificado de lugares consultables. Cacheado 10min — los archivos
    cambian solo cuando se corren los scripts de datos, no en runtime."""
    now = time.time()
    if _places_cache["items"] is not None and now - _places_cache["at"] < 600:
        return _places_cache["items"]

    items: list[dict] = []

    # markers.json — ciudades, embalses manuales
    mk_path = DATA_DIR / "markers.json"
    if mk_path.exists():
        mk = json.loads(mk_path.read_text(encoding="utf-8"))
        for m in mk.get("markers", []):
            items.append({
                "nombre": m["nombre"],
                "tipo": m.get("tipo", "punto"),
                "lat": m["lat"],
                "lon": m["lon"],
                "fuente": "markers",
            })

    # places_risk.geojson — localidades OSM con clasificación de riesgo
    pr_path = HYDRO_DIR / "places_risk.geojson"
    if pr_path.exists():
        gj = json.loads(pr_path.read_text(encoding="utf-8"))
        for f in gj.get("features", []):
            props = f.get("properties", {})
            geom = f.get("geometry", {})
            if geom.get("type") != "Point":
                continue
            lon, lat = geom["coordinates"][0], geom["coordinates"][1]
            items.append({
                "nombre": props.get("nombre", ""),
                "tipo": props.get("tipo", "localidad"),
                "lat": lat,
                "lon": lon,
                "poblacion": props.get("poblacion"),
                "riesgo": props.get("riesgo"),
                "fuente": "places_risk",
            })

    # embalses (polígonos) — usamos centroide para volar la cámara
    res_path = HYDRO_DIR / "reservoirs.geojson"
    if res_path.exists():
        gj = json.loads(res_path.read_text(encoding="utf-8"))
        for f in gj.get("features", []):
            lat, lon = _centroid_of(f.get("geometry"))
            if lat is None:
                continue
            items.append({
                "nombre": f.get("properties", {}).get("nombre", "Embalse"),
                "tipo": "embalse",
                "lat": lat,
                "lon": lon,
                "fuente": "reservoirs",
            })

    # waterways con nombre — un solo punto por curso (centroide de la línea)
    ww_path = HYDRO_DIR / "osm_waterways.simple.geojson"
    if not ww_path.exists():
        ww_path = HYDRO_DIR / "osm_waterways.geojson"
    if ww_path.exists():
        gj = json.loads(ww_path.read_text(encoding="utf-8"))
        seen_names: set[str] = set()
        for f in gj.get("features", []):
            nombre = f.get("properties", {}).get("nombre", "")
            if not nombre or nombre in seen_names:
                continue
            lat, lon = _centroid_of(f.get("geometry"))
            if lat is None:
                continue
            seen_names.add(nombre)
            items.append({
                "nombre": nombre,
                "tipo": f.get("properties", {}).get("tipo", "curso"),
                "lat": lat,
                "lon": lon,
                "fuente": "waterways",
            })

    _places_cache["items"] = items
    _places_cache["at"] = now
    return items


def _centroid_of(geom: dict | None) -> tuple[float | None, float | None]:
    """Centroide barato (promedio de coords) — suficiente para volar cámara."""
    if not geom:
        return None, None
    t = geom.get("type")
    coords = geom.get("coordinates")
    if not coords:
        return None, None
    pts: list[tuple[float, float]] = []

    def walk(c):
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
            if len(c) >= 2:
                pts.append((c[0], c[1]))
        elif isinstance(c, (list, tuple)):
            for sub in c:
                walk(sub)

    walk(coords)
    if not pts:
        return None, None
    n = len(pts)
    lat = sum(p[1] for p in pts) / n
    lon = sum(p[0] for p in pts) / n
    return lat, lon


def _score_match(query: str, name: str) -> float:
    q, n = _normalize(query), _normalize(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    # tokens que se comparten (ignora "rio", "estero", "quebrada" solos)
    stop = {"rio", "río", "estero", "quebrada", "embalse", "el", "la", "los", "las", "de", "del"}
    qtok = [t for t in q.split() if t not in stop]
    ntok = [t for t in n.split() if t not in stop]
    if not qtok:
        qtok = q.split()
    if not ntok:
        ntok = n.split()
    common = set(qtok) & set(ntok)
    if not common:
        # substring fallback
        if q in n or n in q:
            return 0.4
        return 0.0
    score = len(common) / max(len(qtok), len(ntok))
    if n.startswith(q) or q.startswith(n):
        score += 0.1
    return min(score, 0.99)


def tool_buscar_lugar(nombre: str) -> dict:
    items = _load_places()
    scored = []
    for it in items:
        s = _score_match(nombre, it["nombre"])
        if s > 0:
            scored.append((s, it))
    scored.sort(key=lambda x: -x[0])
    top = [
        {
            "nombre": it["nombre"],
            "tipo": it["tipo"],
            "lat": round(it["lat"], 4),
            "lon": round(it["lon"], 4),
            "fuente": it["fuente"],
            "score": round(s, 2),
            **({"poblacion": it["poblacion"]} if it.get("poblacion") else {}),
            **({"riesgo": it["riesgo"]} if it.get("riesgo") else {}),
        }
        for s, it in scored[:5]
    ]
    return {"query": nombre, "candidatos": top}


def _flood_summary(result: dict) -> dict:
    """Aligerar el payload que ve el modelo — la geometría no le sirve."""
    return {k: v for k, v in result.items() if k != "geojson"}


def tool_simular_lluvia(mm: float) -> dict:
    r = estimate_flood(float(mm))
    return _flood_summary(r)


def tool_simular_nivel(delta_h_m: float) -> dict:
    r = estimate_flood_at_level(float(delta_h_m))
    return _flood_summary(r)


def tool_pronostico_dia(dia: int) -> dict:
    fc = fetch_forecast_7d()
    dia = max(1, min(int(dia), 7))
    cum = sum(d["mm"] for d in fc["days"][:dia])
    r = estimate_flood(cum)
    r["forecast_day"] = dia
    r["cumulative_rainfall_mm"] = round(cum, 1)
    return _flood_summary(r)


def tool_evaluar_relaves(delta_h_m: float | None = None, rainfall_mm: float | None = None) -> dict:
    """Aplica las mismas reglas de trigger que el frontend (createRelavesModule
    en app.js) para reportar cuántos relaves quedarían en cada estado."""
    p = HYDRO_DIR / "relaves.geojson"
    if not p.exists():
        return {"error": "Sin catastro de relaves. Correr scripts/11_fetch_relaves.py."}
    gj = json.loads(p.read_text(encoding="utf-8"))
    dh = float(delta_h_m or 0)
    rain = float(rainfall_mm or 0)

    counts = {"dormido": 0, "en_riesgo": 0, "vertiendo": 0}
    spilling = []
    for f in gj.get("features", []):
        props = f["properties"]
        rg = props.get("riesgo_geometrico")
        status = "dormido"
        if rg == "alto" and dh > 2:                        status = "vertiendo"
        elif rg != "bajo" and rain > 80:                   status = "vertiendo"
        elif dh > 6 and rg != "bajo":                      status = "vertiendo"
        elif rg == "alto" and (dh > 0.5 or rain > 40):     status = "en_riesgo"
        elif rg != "bajo" and rain > 60:                   status = "en_riesgo"
        counts[status] += 1
        if status == "vertiendo":
            spilling.append({
                "nombre": props.get("nombre"),
                "empresa": props.get("empresa"),
                "comuna": props.get("comuna"),
                "rio_cercano": props.get("rio_cercano"),
                "dist_rio_m": props.get("dist_rio_m"),
            })
    # devolvemos hasta 10 críticos ordenados por proximidad al río
    spilling.sort(key=lambda x: x["dist_rio_m"] or 1e9)
    return {
        "total": sum(counts.values()),
        "conteos": counts,
        "top_vertiendo": spilling[:10],
        "criterios_aplicados": {"delta_h_m": dh, "rainfall_mm": rain},
    }


def tool_situacion_actual() -> dict:
    p = HYDRO_DIR / "situacion_actual.json"
    if not p.exists():
        return {"disponible": False}
    return json.loads(p.read_text(encoding="utf-8"))


def tool_lluvia_actual() -> dict:
    return fetch_rainfall_24h()


def tool_pronostico_7d() -> dict:
    return fetch_forecast_7d()


TOOL_IMPL = {
    "buscar_lugar": lambda a: tool_buscar_lugar(a["nombre"]),
    "simular_lluvia": lambda a: tool_simular_lluvia(a["mm"]),
    "simular_nivel": lambda a: tool_simular_nivel(a["delta_h_m"]),
    "pronostico_dia": lambda a: tool_pronostico_dia(a["dia"]),
    "ver_terreno_en": lambda a: {"lat": a["lat"], "lon": a["lon"], "nombre": a.get("nombre", "")},
    "evaluar_relaves": lambda a: tool_evaluar_relaves(a.get("delta_h_m"), a.get("rainfall_mm")),
    "situacion_actual": lambda a: tool_situacion_actual(),
    "lluvia_actual": lambda a: tool_lluvia_actual(),
    "pronostico_7d": lambda a: tool_pronostico_7d(),
}


def _tool_to_action(name: str, args: dict, result: dict) -> dict | None:
    """Traduce un tool_use del modelo a una acción concreta que el visor ejecute.
    Devuelve None si esa herramienta no cambia la UI."""
    if name == "buscar_lugar":
        top = (result.get("candidatos") or [])
        if not top:
            return None
        best = top[0]
        return {"type": "fly_to", "lat": best["lat"], "lon": best["lon"], "label": best["nombre"]}
    if name == "simular_lluvia":
        mm = float(args.get("mm", 0))
        return {"type": "set_rain_sim", "mm": mm, "delta_h_m": result.get("delta_h_m")}
    if name == "simular_nivel":
        return {"type": "set_water_slider", "delta_h_m": float(args.get("delta_h_m", 0))}
    if name == "pronostico_dia":
        return {"type": "set_forecast_day", "dia": int(args.get("dia", 1)),
                "delta_h_m": result.get("delta_h_m")}
    if name == "ver_terreno_en":
        return {"type": "open_terrain_profile", "lat": float(args["lat"]),
                "lon": float(args["lon"]),
                "label": args.get("nombre") or "Punto consultado"}
    return None


def _check_rate_limit(ip: str) -> tuple[bool, float]:
    now = time.time()
    with _rate_lock:
        q = _rate_state[ip]
        while q and now - q[0] > RATE_LIMIT_WINDOW_S:
            q.popleft()
        if len(q) >= RATE_LIMIT_MAX:
            retry_after = RATE_LIMIT_WINDOW_S - (now - q[0])
            return False, max(1.0, retry_after)
        q.append(now)
        if len(_rate_state) > _MAX_TRACKED:        # IPs de una sola visita nunca vacían su cola
            for k in [k for k, v in _rate_state.items() if not v or now - v[-1] > RATE_LIMIT_WINDOW_S]:
                del _rate_state[k]
    return True, 0.0


def _clean_history(history: list[dict]) -> list[dict]:
    """Sanea el historial que manda el cliente: descarta cualquier cosa que no
    sea un turno user/assistant de texto simple, y limita el largo."""
    cleaned: list[dict] = []
    for m in history or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant"):
            continue
        if not isinstance(content, str):
            continue
        cleaned.append({"role": role, "content": content[:MAX_MESSAGE_CHARS]})
    # nos quedamos solo con los últimos N turnos (2N mensajes)
    return cleaned[-(MAX_HISTORY_TURNS * 2):]


def chat(user_message: str, history: list[dict], ip: str) -> dict:
    """Un turno de conversación: corre el tool-use loop y devuelve texto +
    acciones que la UI debe ejecutar."""
    if not user_message or not user_message.strip():
        raise ValueError("Mensaje vacío.")
    if len(user_message) > MAX_MESSAGE_CHARS:
        raise ValueError(f"Mensaje demasiado largo (máx {MAX_MESSAGE_CHARS} caracteres).")

    ok, retry_after = _check_rate_limit(ip)
    if not ok:
        raise RateLimitError(f"Demasiadas preguntas. Esperá {int(retry_after)}s.")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "Falta ANTHROPIC_API_KEY. Agrega tu clave de https://console.anthropic.com "
            "al archivo .env (ANTHROPIC_API_KEY=sk-ant-...) y reinicia el servidor."
        )

    with _lock_for(ip):
        return _chat_locked(user_message.strip(), _clean_history(history))


class RateLimitError(Exception):
    pass


def _chat_locked(user_message: str, history: list[dict]) -> dict:
    client = anthropic.Anthropic(timeout=CLIENT_TIMEOUT_S, max_retries=1)
    messages: list[dict] = list(history) + [{"role": "user", "content": user_message}]

    actions: list[dict] = []
    tool_trace: list[dict] = []
    total_in = 0
    total_out = 0

    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )
        total_in += response.usage.input_tokens
        total_out += response.usage.output_tokens

        # apilar el turno del asistente completo (texto + tool_use blocks)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            # respuesta final — extraer texto
            text_parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
            reply_text = "\n\n".join(text_parts).strip() or "(sin respuesta)"
            return {
                "reply": reply_text,
                "actions": actions,
                "tool_trace": tool_trace,
                "usage": {"input_tokens": total_in, "output_tokens": total_out},
                "model": response.model,
            }

        # ejecutar todos los tool_use del turno y devolver los resultados juntos
        tool_results: list[dict] = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            name = block.name
            args = block.input or {}
            impl = TOOL_IMPL.get(name)
            if impl is None:
                result_payload = {"error": f"Herramienta desconocida: {name}"}
                is_error = True
            else:
                try:
                    result_payload = impl(args)
                    is_error = False
                except Exception as exc:
                    result_payload = {"error": f"{type(exc).__name__}: {exc}"}
                    is_error = True

            if not is_error:
                action = _tool_to_action(name, args, result_payload)
                if action:
                    actions.append(action)
            tool_trace.append({"tool": name, "input": args, "ok": not is_error})

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result_payload, ensure_ascii=False, default=str),
                **({"is_error": True} if is_error else {}),
            })

        if not tool_results:
            # el modelo dijo tool_use pero no emitió bloques — cortamos para no colgar
            break
        messages.append({"role": "user", "content": tool_results})

    # se agotó el límite de iteraciones
    return {
        "reply": (
            "Me quedé sin iteraciones de herramientas antes de terminar. "
            "Reformulá la pregunta más acotada, por favor."
        ),
        "actions": actions,
        "tool_trace": tool_trace,
        "usage": {"input_tokens": total_in, "output_tokens": total_out},
        "model": MODEL,
        "truncated": True,
    }
