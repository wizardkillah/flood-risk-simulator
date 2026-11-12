# Documento técnico detallado

Simulador de crecidas — Región de Coquimbo (prototipo).
Última actualización: 2026-07-21.

Este documento describe **cómo está construido** el sistema: arquitectura,
pipeline de datos, endpoints, decisiones técnicas y los "gotchas" resueltos.
Está pensado para que otro desarrollador pueda tomar el proyecto sin pasar
por todos los ensayo-error de las sesiones anteriores.

Para el contexto de riesgo regional y usos futuros ver
[INFORME_TECNICO.md](INFORME_TECNICO.md). Para hallazgos pendientes ver
[AUDITORIA.md](AUDITORIA.md).

---

## 1. Arquitectura de alto nivel

```
   Fuentes externas                Pipeline (offline)              Servicio en vivo
   ──────────────────              ──────────────────              ──────────────────

   OpenTopography (DEM)   ─┐
   HydroBASINS (cuencas)   │
   OpenStreetMap/Overpass  ├──►  scripts/01→11 (Python)   ──►   data/*  (GeoJSON,
   Esri (imagería tiles)   │     ejecución manual una           GeoTIFF, quantized-
   Open-Meteo (lluvia)    ─┘     vez, o al ampliar bbox         mesh, JSON de caché)
                                                                          │
                                                                          ▼
                                                              FastAPI (backend/)
                                                              lee data/, sirve
                                                              endpoints REST y
                                                              el frontend estático
                                                                          │
                                                                          ▼
   Anthropic API ─────────────────────────────────────────►  CesiumJS + Three.js
   Open-Meteo (lluvia en vivo, con caché 15min)              (frontend/, sin build)
   Esri World Imagery (tiles, en vivo)                       navegador del usuario
```

**Principio rector**: todo lo pesado (DEM, cuencas, redes hídricas, teselas
3D) se pre-computa una vez con los `scripts/` y se guarda en `data/`. El
backend en tiempo real solo lee esos artefactos, agrega lluvia vía Open-Meteo
y calcula la mancha de inundación por búsqueda binaria sobre HAND en memoria.
El frontend es HTML + JS puro, sin bundler ni framework.

---

## 2. Estructura del repositorio

```
Simulador crecidas de rio/
├── .env                    # secretos reales (git-ignored)
├── .env.example            # plantilla — vacío en git
├── .gitignore              # ignora .venv, data pesada, .env
├── iniciar.bat / .ps1 / .sh  # launchers (Win/PS/Unix)
├── requirements.txt        # deps mínimas
├── requirements.lock.txt   # snapshot exacto del entorno que funciona
├── README.md
├── backend/
│   ├── app.py              # FastAPI — endpoints + servido de frontend
│   ├── flood.py            # modelo HAND + búsqueda binaria + caché LRU
│   ├── rainfall.py         # cliente Open-Meteo (24h + 7d)
│   ├── rain_history.py     # descarga histórico ERA5 y calcula percentiles
│   ├── terrain_profile.py  # recorte local del DEM para el modal 3D
│   └── ai_advisor.py       # asesor con Claude (JSON estructurado)
├── frontend/
│   ├── index.html
│   ├── css/style.css
│   └── js/app.js           # todo el visor: 1.5K líneas, sin bundler
├── scripts/
│   ├── 01_delimit_basin.py       # HydroBASINS -> 3 cuencas región
│   ├── 02_download_dem.py        # OpenTopography COP30
│   ├── 03_process_terrain.py     # DEM -> teselas quantized-mesh z0..z12
│   ├── 05_compute_hand.py        # pysheds -> HAND raster + red DEM
│   ├── 06_fetch_reservoirs.py    # OSM -> embalses con geometría real
│   ├── 07_delimit_subbasins.py   # HydroBASINS nivel 9 -> subcuencas
│   ├── 08_fetch_osm_hydro.py     # OSM waterways + badenes + pasos a nivel
│   ├── 09_fetch_places_risk.py   # OSM places + clasificación de riesgo
│   ├── 10_analyze_quebradas.py   # intermitentes + confluencias a ríos
│   └── 11_simplify_layers.py     # Douglas-Peucker de las capas pesadas
├── data/
│   ├── dem/                # DEM crudo (325 MB)
│   ├── terrain_tiles/      # 5.316 teselas quantized-mesh + layer.json
│   ├── hydro/              # todos los GeoJSON + situacion_actual.json
│   └── flood/              # HAND + red DEM (~150m UTM)
├── docs/
│   ├── TECNICO_DETALLADO.md  (este)
│   ├── AUDITORIA.md
│   ├── INFORME_TECNICO.md
│   └── HISTORIAL_CHAT.md
└── tests/
    ├── test_numeric.py     # 9 tests puros (bisección, percentiles, bordes)
    └── test_api_smoke.py   # 3 tests de rutas principales
```

---

## 3. Pipeline de datos (scripts 01→11)

Los scripts se corren **manualmente en orden**. Sus salidas viven en `data/`
y son la fuente de verdad para el backend en tiempo real. Se re-corren solo
al ampliar el bbox o cuando OSM cambia mucho.

### 3.1 `01_delimit_basin.py` — delimitación de cuencas

- Descarga (una vez, ~330 MB) el zip `hybas_sa_lev01-12_v1c` de HydroBASINS.
- Selección por **puntos ancla**: Vicuña (Elqui), Ovalle (Limarí), Salamanca
  (Choapa). Para cada uno busca el polígono nivel Pfafstetter 7 que lo
  contiene, extrae su `MAIN_BAS` y disuelve todos los sub-polígonos que
  drenan al mismo punto de salida.
- **Salidas**:
  - `data/hydro/limari_basin.geojson` — nombre confuso mantenido por
    compatibilidad con `flood.py`. Ahora tiene 3 features (Elqui / Limarí /
    Choapa) con `nombre` y `main_bas`.
  - `data/hydro/limari_bbox.json` — bbox regional fijo, cubre las tres
    cuencas + costa + cabeceras.

**Supuestos frágiles**: las coordenadas ancla vienen de memoria. Cuencas
costeras menores (Los Choros, Quilimarí, quebradas costeras La Serena–
Coquimbo) no quedan delimitadas — el bbox regional las incluye visualmente,
pero el modelo HAND solo opera dentro de las 3 cuencas.

### 3.2 `02_download_dem.py` — descarga del DEM

- Requiere `OPENTOPOGRAPHY_API_KEY` en `.env`.
- Consulta `demtype=COP30` (Copernicus GLO-30) con el bbox regional + 0.05°
  de padding.
- **Salida**: `data/dem/limari_cop30.tif` (~325 MB, 7740×12420 px).

**Datum vertical**: COP30 está referenciado al geoide EGM2008, Cesium espera
altura elipsoidal WGS84 (diferencia ~30–40 m en la zona). No se corrige — el
relieve relativo se ve bien; las cotas absolutas están desplazadas.

### 3.3 `03_process_terrain.py` — teselas de terreno 3D

- Genera teselas quantized-mesh **z0 hasta z12** (5.316 teselas totales,
  ~76 m de muestreo en z12 — aprovecha toda la resolución del DEM).
- Pipeline: DEM → grilla regular (65×65) por tesela → malla triangulada con
  `pymartini` → codificación con `quantized-mesh-encoder`.
- **Gotcha crítico #1**: `pymartini.get_mesh` devuelve índices de grilla
  (col, row), NO alturas. Hay que resolver lon/lat/altura desde esos índices
  y el `terrain` original.
- **Gotcha crítico #2**: el eje Y de `available` en `layer.json` usa la
  convención nativa de Cesium (0 = norte), aunque los archivos `.terrain` en
  disco usan Y estilo TMS (0 = sur). Son ejes opuestos, hay que invertir uno
  al escribir el otro.
- **Gotcha crítico #3**: se reemplazó `dataset.sample()` punto a punto
  (cuello de botella al subir z11→z12) por lectura de ventana + interpolación
  bilineal vectorizada (`sample_grid`). Bordes bit a bit iguales entre
  teselas vecinas para no dejar grietas visibles (hay test que lo verifica).
- **Salida**: `data/terrain_tiles/{z}/{x}/{y}.terrain` + `layer.json`.

### 3.4 `05_compute_hand.py` — HAND + red de río del DEM

- Reproyecta el DEM a UTM 19S y remuestrea a 150 m.
- Usa `pysheds` para:
  1. Rellenar depresiones (`fill_pits`, `fill_depressions`).
  2. Calcular dirección de flujo D8 (`flowdir`).
  3. Acumulación de flujo (`accumulation`).
  4. Máscara de cauce donde `accumulation > STREAM_THRESHOLD` (10.000
     celdas = 225 km² aguas arriba).
  5. HAND = altura sobre el drenaje más cercano.
- **Gotcha**: `pysheds` 0.5 usa `np.in1d` que fue removido en numpy 2.0.
  Se aplica shim `np.in1d = np.isin` antes de importar pysheds.
- **Salidas**:
  - `data/flood/dem_utm150m.tif` — DEM reproyectado, insumo de HAND.
  - `data/flood/stream_mask.tif` — máscara binaria del cauce.
  - `data/flood/hand.tif` — HAND en metros.
  - `data/flood/river_network.geojson` — red derivada del DEM (170
    segmentos regionales; ahora es capa opcional, la red OSM es la principal).

**Sobre `STREAM_THRESHOLD = 10000`**: cambio importante de sesiones
anteriores. Antes estaba en 500 (11 km²) y casi cualquier gully contaba como
río, dejando gran parte del terreno con HAND≈0 y una "inundación base" de
~307 km² incluso con 0 mm de lluvia. Subirlo a 10.000 dio inundaciones
proporcionales a la lluvia y una red mucho más limpia.

### 3.5 `06_fetch_reservoirs.py` — embalses de OSM

- Consulta Overpass por `water=reservoir`, `natural=water` con nombre
  Embalse/Tranque, y `landuse=reservoir` en el bbox regional.
- Consultas dirigidas adicionales para La Paloma y Cogotí (mapeo OSM
  irregular en esas coordenadas).
- Une por nombre (dissolve): un embalse mapeado en piezas queda como
  polígono único.
- **Salida**: `data/hydro/reservoirs.geojson` (28 embalses; los grandes:
  Puclaro 6.79 km², Recoleta 4.82, Corrales 2.22, La Laguna 2.0, El Bato,
  Culimo, La Paloma parcial).

**Pendiente**: Cogotí ausente por mapeo OSM incompleto — habrá que agregar
consulta dirigida específica cuando se aborde.

### 3.6 `07_delimit_subbasins.py` — subcuencas nivel 9

- Igual que `01_delimit_basin.py` pero nivel Pfafstetter 9 (más fino), sin
  disolver — deja las subcuencas separadas.
- **Salida**: `data/hydro/subbasins.geojson` — 101 subcuencas (Elqui 35,
  Limarí 37, Choapa 29), cada una con su `HYBAS_ID` y campo `cuenca`.

**Sin nombres**: HydroBASINS no trae etiquetas — no se sabe cuál corresponde
a Río Grande, Hurtado, Rapel/Cogotí, Guatulame sin cruzar a mano contra un
mapa oficial.

### 3.7 `08_fetch_osm_hydro.py` — red hídrica + cruces

- Overpass por `waterway~"^(river|stream|canal|drain)$"` en el bbox regional
  (4.855 tramos).
- Overpass separada por badenes y pasos a nivel (**3 consultas separadas
  para no hacer timeout** — la clave `ford` sin valor específico da 504 en
  todos los espejos).
- **Salidas**:
  - `data/hydro/osm_waterways.geojson` — 4.855 tramos, propiedades: nombre,
    tipo, intermitente (bool).
  - `data/hydro/osm_crossings.geojson` — 1.856 puntos (1.743 badenes,
    113 pasos a nivel).

**Gotcha Overpass**: la consulta `node["ford"]` (por existencia de clave) da
504 sostenido; hay que consultar por valor `node["ford"="yes"]`. También el
User-Agent identificable es requerido por la política de uso.

### 3.8 `09_fetch_places_risk.py` — comunidades + riesgo

- Overpass por `place=city|town|village|hamlet` en el bbox regional
  (1.141 lugares).
- Calcula distancia (en UTM = metros reales) de cada lugar al río o
  quebrada natural más cercano.
- **Clasificación** (proxy geométrico, NO oficial):
  - **alto**: <200 m de un río permanente O <120 m de una quebrada
    intermitente.
  - **medio**: <600 m de cualquier curso natural.
  - **bajo**: el resto.
- **Salida**: `data/hydro/places_risk.geojson` — 242 en riesgo alto (ej.
  Salamanca a 120 m del Choapa; Andacollo a 53 m de una quebrada).

**Limitación honesta**: es distancia horizontal, no elevación relativa al
cauce (HAND). Un pueblo en un barranco alto a 150 m del río queda "alto"
aunque no se inunde nunca. Mejora natural: cruzar con HAND regional.

### 3.9 `10_analyze_quebradas.py` — intermitentes + confluencias

- Separa las quebradas intermitentes (`intermittent=yes`) del resto.
- Calcula **confluencias**: extremos de quebradas intermitentes a <150 m
  de un río permanente. Cada punto lleva nombre de la quebrada y del río
  receptor (via `sjoin_nearest`, con deduplicación de índices para manejar
  empates de distancia).
- **Salidas**:
  - `data/hydro/quebradas_intermitentes.geojson` — 3.695 tramos.
  - `data/hydro/confluencias.geojson` — 557 puntos (ej. `Río Chalinga →
    Río Choapa` = desborde real en Salamanca de julio 2026).

### 3.10 `11_simplify_layers.py` — versiones livianas para el visor

- Douglas-Peucker en UTM (150 m ríos, 200 m quebradas — imperceptible en
  el visor regional pero baja el peso 20-60x).
- Filtra quebradas cortas sin nombre (guarda solo las con nombre o >= 1 km).
- **Salidas**:
  - `osm_waterways.simple.geojson` — solo ríos y esteros permanentes (sin
    intermitentes ni canales). 805 tramos, **287 KB** (era 17 MB).
  - `quebradas_intermitentes.simple.geojson` — 1.989 tramos, **582 KB**
    (era 12 MB).

Los originales se mantienen — el backend sirve `.simple.geojson` por
defecto y el crudo con `?full=true` para análisis.

---

## 4. Backend (FastAPI)

`backend/app.py` es el único módulo con rutas. Todo el estado vive en memoria
del proceso (adecuado mientras corra en localhost mono-worker; ver
AUDITORIA.md A3 para multi-worker).

### 4.1 Endpoints

| Endpoint | Método | Descripción |
|---|---|---|
| `/` | GET | Sirve `frontend/index.html` |
| `/terrain/layer.json` | GET | Metadata de teselas |
| `/terrain/{z}/{x}/{y}.terrain` | GET | Tesela quantized-mesh (gzip) |
| `/api/basin` | GET | 3 cuencas delimitadas |
| `/api/markers` | GET | Ovalle |
| `/api/rainfall` | GET | Lluvia 24h grilla 8×8 (caché 15min) |
| `/api/flood` | GET | Mancha de inundación (auto / delta_h / rainfall_mm) |
| `/api/flood/config` | GET | Max delta_h admitido |
| `/api/flood/forecast_day?day=N` | GET | Mancha para lluvia acumulada al día N |
| `/api/forecast` | GET | Pronóstico diario 7 días |
| `/api/river` | GET | Red derivada del DEM (170 segmentos) |
| `/api/waterways?full=false` | GET | Red OSM permanente (simplificada) |
| `/api/quebradas?full=false` | GET | Quebradas intermitentes (simplificadas) |
| `/api/confluencias` | GET | Descargas quebrada→río |
| `/api/places_risk` | GET | Comunidades clasificadas |
| `/api/crossings` | GET | Badenes + pasos a nivel |
| `/api/situation` | GET | Emergencia actual (prensa) |
| `/api/subbasins` | GET | 101 subcuencas |
| `/api/reservoirs` | GET | 28 embalses |
| `/api/terrain_profile?lat&lon&size_km` | GET | Parche 128×128 del DEM |
| `/api/ai_analysis?refresh=false` | GET | Análisis IA (caché 15min + lock) |

**Nota sobre `StaticFiles`**: `app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True))` debe ir **al final** de `app.py`. Cualquier ruta no capturada arriba sirve el frontend estático.

### 4.2 `backend/flood.py` — el corazón del modelo

**Método**: HAND (Height Above Nearest Drainage) + balance de volumen.

```
runoff_volume_m3 = (rainfall_mm / 1000) * basin_area_m2 * RUNOFF_COEFFICIENT
                                                          # RUNOFF_COEFFICIENT = 0.3 (fijo, inventado)
delta_h = búsqueda binaria tal que
          suma((delta_h - HAND[i]) * cell_area for i where HAND[i] <= delta_h) == runoff_volume_m3
mancha = { celdas i : HAND[i] <= delta_h }
```

Techo: `MAX_DELTA_H_M = 30.0`. Bisección: 30 iteraciones, tolerancia 5%.

**Caché LRU de resultados** (agregada en la sesión de optimización): el
resultado completo (mask + geojson + metadatos) se cachea por `delta_h`
redondeado a 0.1 m. 64 entradas, evicción FIFO. La segunda pasada por el
mismo nivel: 5.4 s → 14 ms.

**Simplificación del geojson** (`_mask_to_geojson`): la rasterización a 150 m
produce polígonos con miles de vértices "en escalera". Se aplica:
1. Descartar fragmentos < 10 celdas (piñas sueltas visualmente confusas).
2. `unary_union` + `simplify(250, preserve_topology=True)` — tolerancia
   agresiva a propósito, la precisión del modelo no la justifica.

**API pública**:
- `estimate_flood(rainfall_mm)` — modo auto/simulado: calcula runoff →
  delta_h → mancha.
- `estimate_flood_at_level(delta_h)` — modo manual: usuario fija delta_h.

### 4.3 `backend/rainfall.py` — Open-Meteo

- Grilla 8×8 = 64 puntos sobre el bbox regional.
- `fetch_rainfall_24h()`: precipitación horaria de las últimas 24 h por
  punto, promedio. Caché 15 min.
- `fetch_forecast_7d()`: sumas diarias promedio 7 días. Caché 15 min.
- Es **salida de modelo** (ICON/ECMWF/GFS "best match"), no medición.
  Resolución ~9-25 km — mucho más gruesa que la cuenca.

### 4.4 `backend/rain_history.py` — histórico ERA5

- `archive-api.open-meteo.com` en un solo punto (Ovalle).
- Desde 2000-01-01 hasta hace 8 días (~9.700 días).
- Calcula percentiles p90/p95/p99, top-10 días más lluviosos, promedio
  anual, días >20mm por año.
- **Cacheado en `data/hydro/rain_history_stats.json`** — el histórico no
  cambia hacia atrás, se refresca solo con `force=True`.

**Validación de los datos**: el récord (91.9mm el 2003-05-21) y el temporal
mayo 2017 (69.6mm el 11-05-2017) aparecen en el top-10, coherente con
memoria histórica de la región.

### 4.5 `backend/terrain_profile.py` — modal 3D

- `get_terrain_profile(lat, lon, size_km=3)`: recorta un parche 128×128 del
  DEM crudo alrededor del punto clickeado.
- Interpolación bilineal (`Resampling.bilinear` de rasterio).
- Devuelve grilla de alturas + min/max + bounds — el frontend lo dibuja en
  Three.js con textura satelital drapeada.

**Limitación conocida**: usa la elevación mínima del parche como referencia
del cauce, no HAND real. Es un modelo local independiente del principal, y
sus % de inundación no son directamente comparables.

### 4.6 `backend/ai_advisor.py` — asesor con Claude

- Modelo: `claude-opus-4-8` (configurable con `AI_ADVISOR_MODEL`).
- **Adaptive thinking** (`thinking={"type": "adaptive"}`) + salida forzada
  a JSON con `output_config.format.json_schema`.
- **Contexto que arma** (~3 KB, sin geometrías): lluvia 24h + pronóstico
  7d + estimación HAND + histórico + situación reportada + comunidades en
  riesgo alto + inventario regional (embalses, cursos con nombre, cruces,
  quebradas). Ver `_build_context()`.
- **Sistema prompt**: pide análisis conservador con formato estructurado
  (nivel de alerta, análisis climático, comparación histórica, zonas en
  riesgo, recomendaciones, evacuación preventiva, disclaimer).
- **Caché 15 min** + **lock** (`threading.Lock`) para que clicks múltiples
  no paguen la llamada varias veces + **piso 60s entre refreshes forzados**.
- **Costo típico**: ~9.500 tokens in / ~2.800 tokens out ≈ $0.05-0.15 USD
  por análisis en Opus 4.8. Reducible ~5x con `claude-sonnet-5`.

---

## 5. Frontend

Sin bundler, sin framework. HTML + CSS + un único `js/app.js` (~1.500 líneas)
que se carga como script clásico. Cache-buster manual (`?v=17`) al final del
tag.

### 5.1 Bibliotecas externas (via CDN)

- **CesiumJS 1.143.0** — mapa 3D + terreno quantized-mesh + capas GeoJSON.
- **Three.js r128** — escena 3D del modal (corte de terreno con agua).

Ninguna requiere cuenta ni token (Cesium ion no se usa; terreno self-hosted;
imagería Esri por tiles públicos).

### 5.2 Arquitectura de capas

**Carga inicial** (4 capas + mancha, ~2-3 segundos):
- `waterways` (287 KB, GeoJSON Entities)
- `situacion` (7 KB, Entities con label — 13 puntos rojos)
- `places` (273 KB, **PointPrimitiveCollection + LabelCollection**)
- `reservoirs` (115 KB, GeoJSON Entities)
- `flood` (45 KB, GeoJSON Entities — se recrea con cada cambio de nivel)

**Bajo demanda** (lazy, se cargan al activar toggle):
- `quebradas` (582 KB, dashed polylines) — 1.989 tramos naranjas
- `confluencias` (110 KB, **PointPrimitiveCollection**) — 557 puntos
- `crossings` (274 KB, **PointPrimitiveCollection**) — 1.856 puntos
- `subbasins` (457 KB, polígonos morados) — 101 subcuencas
- `river` (915 KB, red DEM alternativa) — 170 segmentos

**Estructura del lazy load** (`init()` en `app.js`):
```js
const lazy = {
  river: { fn: () => loadRiverNetwork(viewer), ds: null, loading: null },
  quebradas: { fn: () => loadQuebradas(viewer), ds: null, loading: null },
  ...
};
async function ensureLazy(key) {
  const slot = lazy[key];
  if (slot.ds) return slot.ds;
  if (!slot.loading) slot.loading = slot.fn();
  slot.ds = await slot.loading;
  return slot.ds;
}
```

Los handlers de toggle usan `bindEager(id, ds)` para las cargadas y
`bindLazy(id, key, label)` para las diferidas.

### 5.3 `PointPrimitiveCollection` — decisión clave de rendimiento

Para las 3 capas de puntos (comunidades 1.141 + badenes 1.856 + confluencias
557 = **3.554 puntos**), se usa `Cesium.PointPrimitiveCollection` nativa en
vez de una `Entity` por punto:

```js
const points = viewer.scene.primitives.add(new Cesium.PointPrimitiveCollection());
for (const f of gj.features) {
  points.add({ position: ..., pixelSize: 7, color: ... });
}
```

Un batch único en GPU en vez de 3.554 entidades individuales que cada una
recorre el sistema de picking / batching / render de Cesium. Diferencia
observable: 10-100x en carga y 10-100x en FPS.

Wrapper `makePointLayer()` expone `.show`, `.name`, `.destroy()` para que
los toggles y el resto del código no vean la diferencia.

### 5.4 Modelo de datos del panel principal

- Slider "Nivel manual" (0-25 m, step 0.1) + 3 presets (0.5 / 2 / 6 m).
- Slider "Simular lluvia" (0-300 mm, step 5).
- Botones "día N pronóstico" (7 botones, generados desde `/api/forecast`).
- Botón "Actualizar según lluvia actual" (llama a Open-Meteo con refresh).
- Botón "Generar análisis de riesgo" (llama al asesor IA).

**Debounce**: 150 ms para el slider manual, 200 ms para simular lluvia.
Cada cambio dispara `updateFlood()` que:
1. Incrementa `window.__floodReqId`.
2. Fetch al backend.
3. Chequea si su ID sigue siendo el actual (si no → descarta, remueve el
   DS si ya lo agregó).
4. Reemplaza `window.__floodDataSource` en el viewer.
5. Actualiza el panel + limpia estado de otros controles.

Si dos requests corren en paralelo (arrastre + otro control), solo la última
gana. La primera se descarta pero **remueve su DS del viewer** si lo agregó
(el fix que evita manchas huérfanas — BUG-D).

Si el backend devuelve 0 features (delta_h=0 o similar): la **mancha anterior
se mantiene visible** (BUG-F) en vez de borrar el mapa.

### 5.5 Dibujar río a mano

`setupRiverDrawing()`:
1. Click en botón "✏️ Dibujar río" → entra en modo dibujo.
2. Cada click sobre el mapa → `pickTerrainPosition()` (globe.pick →
   pickPosition → pickEllipsoid como fallback) → agrega un punto azul.
3. Doble click → cierra el trazo:
   - Genera un `CorridorGraphics` con `clampToGround: true` (se amolda al
     relieve) y ancho proporcional a los mm activos.
   - Abre el **modal 3D** en el punto medio del trazo.

**Gotcha resuelto**: `scene.pickPosition` devuelve `null` sobre terreno sin
entidades en muchos entornos (depende del buffer de profundidad). El orden
robusto es `globe.pick(rayo)` primero.

### 5.6 Modal 3D — corte de terreno con agua

Al hacer click en un río o al terminar de dibujar uno, se abre un modal con
una escena Three.js separada:

- **Terreno**: `PlaneGeometry` 128×128, cada vértice desplazado en Y con
  el DEM local (parche 3×3 km). Exageración vertical dinámica según rango.
- **Textura satelital**: `loadSatelliteTexture()` cose las teselas
  web-mercator de Esri sobre el parche y las aplica como textura del mesh.
- **Superficie de agua** que sigue el cauce real: para cada celda, la
  base = elevación mínima en ventana ~300m ("HAND local" aproximado). Con
  nivel bajo, mojarse solo la franja pegada al cauce.
- **Interacción**: arrastrar para rotar (esférica orbital), rueda del mouse
  para zoom (radius 60-700).
- **Lluvia animada**: `THREE.Points` (300-2500 gotas según mm activos) que
  caen sobre la escena.

Ver `initRiverScene()`, `loadSatelliteTexture()`, `applyRiverWaterLevel()`.

### 5.7 Lluvia animada sobre el mapa principal

Canvas 2D superpuesto al visor Cesium (`pointer-events: none`, z-index
900). `setupRainOverlay()` decide la densidad de gotas según mm activos y
si se está dibujando un río. Se anima con `requestAnimationFrame`.

### 5.8 Panel del asesor IA

Estructura del render (`loadAiAnalysis()`):
- Chip de color según nivel de alerta (verde/amarilla/naranja/roja).
- Título breve + resumen.
- Análisis climático + comparación histórica.
- Lista de zonas en riesgo (lugar + motivo).
- Lista de recomendaciones.
- Evacuación preventiva (párrafo explícito).
- Metadata: disclaimer, modelo usado, timestamp, mm activos.

Todos los strings vienen del JSON estructurado del backend y pasan por
`escapeHtml()`.

---

## 6. Dependencias y entorno

`requirements.txt` (intención):
```
rasterio, geopandas, numpy, requests, python-dotenv, fastapi,
uvicorn[standard], pymartini, quantized-mesh-encoder, pysheds, anthropic
```

`requirements.lock.txt`: snapshot exacto del venv que funciona (`pip freeze`).

**Python**: 3.14 en el entorno actual.

**Instalación desde cero**:
```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

**Windows**: `pymartini` y `quantized-mesh-encoder` requieren compilar
extensiones C++, hay que tener Visual C++ Build Tools instalado. Si `vswhere`
no registra la instalación, agregar el directorio del instalador de Visual
Studio al PATH de usuario (`vcvarsall.bat` invoca `vswhere` sin ruta y si no
está en PATH corrompe el parseo de setuptools).

---

## 7. Tests

Suite pequeña (12 tests, todos pasando):

**`tests/test_numeric.py`**:
- Bisección de flood: caso cero, monotonía, tope, consistencia
  volumen/nivel, mask crece con nivel.
- Percentiles de `rain_history._compute_stats`.
- `sample_grid` de terrain: bordes idénticos entre teselas vecinas
  (tolerancia 1mm — ruido de float32 aceptable, quantized-mesh cuantiza
  mucho más grueso).

**`tests/test_api_smoke.py`**:
- `GET /` sirve el frontend con "Simulador" en el HTML.
- `GET /api/flood?delta_h=2` devuelve `mode=manual`, `flooded_area>0`.
- Rutas estáticas (basin, markers, waterways, crossings) responden 200 o
  404 (404 solo si el dato no está generado).

Correr: `.venv\Scripts\python.exe -m pytest tests -q`. Duración: ~7 s.

---

## 8. Configuración

**`.env`** (git-ignored, real):
```
OPENTOPOGRAPHY_API_KEY=<key>
CESIUM_ION_TOKEN=<opcional, no se usa hoy>
ANTHROPIC_API_KEY=<key>
AI_ADVISOR_MODEL=claude-opus-4-8   # opcional
```

**`.env.example`** (versionado, plantilla, vacío).

**`.gitignore`** ignora: `.venv/`, `__pycache__/`, `*.pyc`, `.env`, DEMs,
imagería, HAND, teselas.

---

## 9. Launchers

Tres versiones equivalentes:

- **`iniciar.bat`** — doble click en Windows. ASCII sin BOM (crítico para
  cmd.exe). Verifica `.venv/`, `.env`, `data/flood/hand.tif`, puerto 8000
  libre → arranca `uvicorn` y abre el navegador con 3s de delay.
- **`iniciar.ps1`** — equivalente PowerShell.
- **`iniciar.sh`** — equivalente macOS/Linux (`open` / `xdg-open` según
  plataforma).

Ctrl+C detiene el servidor. Sin `--reload` (para uso normal, no desarrollo).

---

## 10. Gotchas y cosas no obvias resueltas (compilado)

### Pipeline / datos

- `pymartini.get_mesh` devuelve índices de grilla, NO alturas → hay que
  resolver contra `terrain`.
- Eje Y de `layer.json` vs archivos `.terrain` en disco: convenciones
  opuestas (norte=0 vs sur=0).
- `sample_grid` con `dataset.sample()` punto a punto es cuello de botella
  → ventana + bilineal vectorizado, con bordes idénticos entre teselas.
- `pysheds` 0.5 + numpy 2.0: aplicar shim `np.in1d = np.isin` antes de
  importar.
- HAND con `STREAM_THRESHOLD` bajo produce inundación base masiva incluso
  con 0mm de lluvia — usar 10.000 celdas (225 km²) mínimo.
- Overpass: `node["ford"]` sin valor da 504 → usar `["ford"="yes"]`.
- Overpass: User-Agent identificable requerido por política de uso.
- `sjoin_nearest` duplica filas ante empates de distancia → deduplicar
  por índice después.

### Backend

- FastAPI: `HEAD` no se expone automáticamente en rutas `@app.get`. Si el
  frontend chequea existencia con `HEAD`, se rompe. Usar `GET` normal para
  la verificación (relevante para `/terrain/layer.json`).
- `CesiumTerrainProvider.fromUrl` queda en estado inválido si `layer.json`
  no existe → romper el globo entero. Verificar antes.
- Cachés en memoria: OK con mono-worker, se duplican con `--workers N`.
- Modelo Anthropic: `budget_tokens` está removido en Opus 4.7+ (400). Usar
  `thinking: {type: "adaptive"}`. Sampling params (`temperature` etc.)
  también removidos. Prefills de asistente también.

### Frontend

- `scene.pickPosition` retorna null sobre terreno sin entidades → usar
  `globe.pick(rayo)` primero.
- `querySelectorAll(".presetBtn")` matcheaba también los del modal → hay
  que scopear a `#waterPresets .presetBtn`.
- Race condition en `updateFlood`: chequear obsolescencia ANTES de asignar
  `__floodDataSource`, y remover el DS huérfano si perdió la carrera.
- Si respuesta trae 0 features (delta_h=0), mantener la mancha vieja en vez
  de borrar el mapa.
- Cache-buster en `<script src="js/app.js?v=N">` — subirlo al editar.
- Navegador cachea agresivamente `app.js` incluso con hard-reload — un
  cambio de query param obliga la recarga.
- 3.554 puntos como Entities individuales ahogan el renderer → usar
  `PointPrimitiveCollection` nativa (10-100x más rápido).
- Geometrías con miles de vértices por polyline ahogan Cesium en carga y
  en cada frame → Douglas-Peucker offline (script 11).

### Launcher

- Archivos `.bat` con codificación UTF-8+BOM rompen el parser de cmd.exe
  ("M" no se reconoce como comando por línea REM). Escribir en ASCII sin
  BOM.
- `cd /d` en el bat: el sandbox de la sesión bloquea `/d` como si fuera
  argumento de `rm`. Usar `pushd` en su lugar (más seguro además).

---

## 11. Rendimiento (última medición)

Sesión de optimización 2026-07-21. Comparado con versión previa:

| Recurso | Antes | Después | Factor |
|---|---:|---:|---:|
| `/api/waterways` | 17 MB / 4855 tramos | **287 KB / 805** | 60x |
| `/api/quebradas` | 12 MB / 3695 tramos | **582 KB / 1989** | 21x |
| `/api/flood?delta_h=N` (repetido) | 5.4 s | **14 ms** | 385x |
| DataSources al inicio | 12 | **6** | — |
| Puntos como Entities | 3.554 individuales | **3 batches GPU** | 10-100x |
| Tiempo primer render usable | ~20-40 s | **~3-5 s** | 5-10x |

---

## 12. Próximos pasos priorizados (de AUDITORIA.md)

Copia condensada — ver `docs/AUDITORIA.md` completo:

**Alto**:
- **A2**: agregar ~5 tests más de rutas críticas y regresiones. Existen 12,
  cubrir mejor `_solve_delta_h_for_volume` con edge cases.
- **A3**: si pasa a servidor real, mover cachés a Redis/disco compartido.

**Medio**:
- **M2**: unificar el modelo del modal 3D con el HAND regional (hoy son dos
  modelos incompatibles cuyos % no coinciden).
- **M3**: calibrar `RUNOFF_COEFFICIENT` con caudales DGA reales.
- **M4**: manejar mejor los fallos de servicios externos (Open-Meteo,
  Overpass, Esri).

**Bajo**:
- **B1**: partir `app.js` (1.500 líneas) en módulos.
- **B4**: no reabrir el GeoTIFF en cada request de `/api/terrain_profile`.
- Cogotí falta en la capa de embalses (mapeo OSM incompleto).

---

## 13. Datos y URLs útiles

**Fuentes externas**:
- OpenTopography COP30: https://portal.opentopography.org
- HydroBASINS: https://www.hydrosheds.org/products/hydrobasins
- OSM Overpass: https://overpass-api.de/api/interpreter (+ mirrors)
- Esri World Imagery: https://services.arcgisonline.com/arcgis/rest/services/World_Imagery
- Open-Meteo: https://api.open-meteo.com (forecast) + https://archive-api.open-meteo.com (histórico)
- Anthropic API: https://console.anthropic.com

**Endpoints locales** (con servidor corriendo en `localhost:8000`):
- `/` — visor
- `/docs` — swagger de FastAPI (auto-generado)
- `/api/*` — endpoints REST enumerados en §4.1

**Commits relevantes** (branch `master`):
```
fa8f106  Optimizacion de rendimiento
a1d072d  Fixes de sliders/botones + launchers iniciar.bat/.ps1/.sh
4109f57  Ampliacion a toda la Region de Coquimbo + situacion actual
35badce  Correcciones de auditoria
cb992ff  Prototipo simulador de crecidas del Limari - Fase 1 completa
```
