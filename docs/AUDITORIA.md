# Auditoría del proyecto — Simulador de crecidas del río Limarí

Fecha: 2026-07-20. Alcance: todo el repositorio (backend, frontend, scripts de
datos, configuración). Los hallazgos están priorizados: **CRÍTICO** (atender ya),
**ALTO** (antes de mostrar/compartir el proyecto), **MEDIO** (antes de Fase 2),
**BAJO** (deuda técnica tolerable en un prototipo).

---

## CRÍTICO

### C1. Claves reales en `.env.example`
`​.env.example` contiene actualmente una API key real de Anthropic, la key de
OpenTopography y un token de Cesium ion. Ese archivo es la **plantilla** que por
convención se versiona y se comparte (el `.gitignore` excluye `.env`, no
`.env.example`). Hoy el proyecto no es un repo git, así que aún no se filtró
nada — pero el primer `git init && git push` publicaría las tres claves.

**Acción**: mover los valores reales a `.env` (que sí está ignorado), dejar
`.env.example` solo con placeholders vacíos, y **rotar la clave de Anthropic**
en console.anthropic.com (las claves expuestas se consideran comprometidas;
la de Anthropic tiene costo directo asociado). La de OpenTopography es gratuita
pero conviene regenerarla igual.

### C2. El proyecto no está bajo control de versiones
`git: false`. Hay ~20 archivos de código fuente con semanas de trabajo y cero
historial: un `rm` accidental o una edición mala no tienen vuelta atrás.
El `.gitignore` ya está bien pensado (excluye datos pesados y `.env`).

**Acción**: `git init`, primer commit, y de ahí en adelante commits por sesión
de trabajo. Idealmente un remoto privado (GitHub/GitLab) como respaldo.

---

## ALTO

### A1. `/api/ai_analysis` sin control de concurrencia ni de abuso
- **Carrera de costo**: dos requests simultáneas (doble clic, dos pestañas)
  disparan **dos** llamadas reales a Claude — el caché se escribe recién al
  terminar. Con `claude-opus-4-8` cada análisis cuesta dinero real.
- **Sin límite de tasa**: cualquier cliente que alcance el puerto puede forzar
  `?refresh=true` en bucle y generar costo ilimitado.
- **Bloqueo largo**: la llamada tarda 30–90s y ocupa un thread del pool de
  FastAPI todo ese tiempo.

**Acción**: (1) un `threading.Lock` alrededor de `analyze()` para que requests
concurrentes esperen el resultado en vez de duplicar la llamada; (2) un mínimo
entre refreshes forzados (p. ej. 60s) aunque venga `refresh=true`; (3) si el
servidor deja de ser 100% local, autenticación o allowlist de origen antes que
nada.

### A2. Sin ninguna prueba automatizada
Cero tests. La lógica con más riesgo de regresión silenciosa es puramente
numérica y fácil de testear sin red: `flood._solve_delta_h_for_volume`
(bisección), `rain_history._compute_stats` (percentiles), el mapeo
lat/lon→ventana de `terrain_profile`, y `sample_grid` de `03_process_terrain.py`
(la continuidad de bordes entre teselas vecinas — un bug ahí produce grietas
en el terreno 3D que ya costó trabajo evitar).

**Acción**: `pytest` con ~10 tests sobre esas cuatro funciones usando arrays
sintéticos. Un test de humo de la API con `TestClient` de FastAPI (que `/` y
`/api/flood?delta_h=2` respondan 200) cubre el resto barato.

### A3. Estado global del backend no apto para multi-worker
`flood._state`, los cachés de `rainfall.py` y el de `ai_advisor` son
diccionarios en memoria del proceso. Con `uvicorn --workers N` (o detrás de
gunicorn) cada worker tendría su propio caché → N llamadas a Open-Meteo/Claude,
y la carga lazy de `_load()` no tiene lock (dos requests simultáneas la
ejecutan dos veces; benigno pero desprolijo).

**Acción**: mientras sea mono-proceso local no hay urgencia; documentarlo con
un comentario. Si pasa a servidor real: caché compartido (disco o Redis) y
`lifespan` de FastAPI para cargar HAND al arranque en vez de lazy.

---

## MEDIO

### M1. Rendimiento de la capa OSM (1194 polilíneas clamped)
Cada tramo es una entidad `GroundPolyline` individual. En GPUs modestas la
carga inicial y el picking pueden notarse. Además los 328 puntos de cruces
usan `disableDepthTestDistance: Infinity` (siempre visibles a través del
terreno).

**Acción**: si se nota lento — agrupar por tipo en `PolylineCollection` /
primitivas por lotes, o filtrar por nivel de zoom (mostrar quebradas solo al
acercarse). Medir primero: en la GPU del equipo actual funciona.

### M2. El modal 3D y el mapa principal usan modelos de inundación distintos
Documentado en el README, pero sigue siendo la inconsistencia conceptual más
visible: el panel principal usa HAND de cuenca completa; el modal usa el
"HAND local" aproximado (min en ventana de 300m). Los porcentajes no son
comparables entre sí y un usuario los va a comparar.

**Acción Fase 2**: servir el raster HAND real recortado al parche desde el
backend (`/api/terrain_profile` ya recorta el DEM; puede recortar HAND igual)
y usar una sola referencia.

### M3. Escorrentía instantánea y sin embalses (límite del modelo, ya documentado)
El coeficiente 0.3 fijo, el equilibrio instantáneo y la ausencia de los tres
embalses hacen que el área inundada sea una cota superior gruesa. Está bien
divulgado en README y disclaimers — el riesgo es que la interfaz se ve más
seria que el modelo. La mitigación de corto plazo ya existe (disclaimers);
la real es la calibración con caudales DGA (Fase 2).

### M4. Dependencia total de servicios externos vivos
Imagería Esri, Open-Meteo (3 endpoints), Overpass y la API de Anthropic. Sin
internet el visor pierde la textura satelital y toda la parte "en vivo".
Open-Meteo sin API key tiene límites de cortesía; Esri World Imagery permite
uso ligero pero su ToS exige atribución (ya está) y no cachear masivamente.

**Acción**: manejar el fallo de cada fetch con mensaje claro en el panel (hoy
algunos solo hacen `console.warn`). Para demos sin internet: pre-descargar un
mosaico pequeño de imagería o un estilo de respaldo con contornos.

### M5. Codificación Windows-1252 en salidas de consola
Varios scripts imprimen UTF-8 que la consola Windows corrompe (`rean�lisis`);
`08_fetch_osm_hydro.py` terminó con exit code 1 por eso pese a funcionar, y
`rain_history.py` con 255. Confunde diagnósticos.

**Acción**: `sys.stdout.reconfigure(encoding="utf-8")` al inicio de los
scripts, o `PYTHONIOENCODING=utf-8` en el entorno.

### M6. `requirements.txt` sin versiones fijadas
Ya se agregó `anthropic` (faltaba). Pero nada tiene versión: una reinstalación
en 6 meses traerá numpy/rasterio/pysheds distintos, y pysheds ya demostró ser
frágil (shim `np.in1d`, downgrade de numpy documentado en README).

**Acción**: `pip freeze > requirements.lock.txt` como foto del entorno que
funciona, manteniendo `requirements.txt` como lista de intención.

---

## BAJO

- **B1. `app.js` monolítico (1212 líneas)**: dibujo, capas, lluvia, modal 3D,
  panel IA — todo junto, y ~10 variables `window.__*` como estado global.
  Funciona, pero cada feature nueva cuesta más. Partir en módulos ES
  (`capas.js`, `dibujo.js`, `modal3d.js`, `ia.js`) cuando haya una pausa.
- ~~**B2. `updatePanel`~~ (corregido: ahora escapa con `escapeHtml`) interpola datos del backend con `innerHTML`** sin
  escapar. Hoy el backend es propio y local (riesgo real casi nulo) y el panel
  IA nuevo sí escapa con `escapeHtml` — unificar criterio.
- **B3. Umbrales duplicados**: `floodStatusLabel` (JS) y los presets 0.5/2/6m
  están hardcodeados en dos archivos (index.html y app.js) y no salen de
  ninguna fuente única. Mover a un `/api/config`.
- **B4. `terrain_profile` lee el GeoTIFF completo en cada clic** (abre el
  archivo, sin caché de dataset). Imperceptible hoy; `rasterio.open` con caché
  de proceso si algún día molesta.
- **B5. Badenes tipo nodo con `ford` distinto de `yes`** (`stepping_stones`,
  etc.) quedan fuera de la consulta Overpass (documentado en el script).
- **B6. Coordenadas de Ovalle/embalses sin verificar** contra fuente oficial
  (marcado con `*` en el visor y en README desde Fase 1 — sigue pendiente).
- **B7. Accesibilidad/móvil**: paneles con fuentes de 10–13px, sin diseño
  responsive ni navegación por teclado. Irrelevante para el prototipo de
  escritorio; relevante si esto se muestra a terceros en terreno.

---

## Lo que está bien (para conservar)

- **Honestidad del modelo**: disclaimers consistentes en UI, API y README;
  simplificación deliberada de polígonos para no aparentar precisión falsa.
- **Documentación**: el README registra decisiones, supuestos no verificados y
  gotchas técnicos con un nivel de detalle poco común en prototipos.
- **Pipeline de datos reproducible**: scripts numerados 01→08 con fuentes
  públicas y caché local; regenerable de cero en <1 hora.
- **Elecciones técnicas sobrias**: terreno self-hosted sin cuentas de pago,
  quantized-mesh estándar, FastAPI mínimo, sin framework de frontend que
  envejezca mal.
- **El asesor IA entrega contexto compacto y verificable** (~3KB, sin
  geometrías) con datos históricos reales, y fuerza salida estructurada.

## Resumen priorizado

| # | Hallazgo | Esfuerzo | Impacto |
|---|---|---|---|
| C1 | Claves reales en `.env.example` — mover y **rotar** | 10 min | Evita filtración/costo |
| C2 | `git init` + primer commit | 10 min | Protege todo el trabajo |
| A1 | Lock + rate-limit en `/api/ai_analysis` | 1 h | Evita costo duplicado |
| A2 | ~10 tests numéricos + smoke test API | 3 h | Regresiones silenciosas |
| A3 | Documentar/preparar cachés para multi-worker | 1 h | Solo si deja de ser local |
| M1–M6 | Ver arriba | variable | Calidad Fase 2 |
