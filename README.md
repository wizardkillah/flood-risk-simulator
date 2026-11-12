# Simulador de crecidas — Región de Coquimbo (prototipo)

[![CI](https://github.com/wizardkillah/flood-risk-simulator/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/wizardkillah/flood-risk-simulator/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Base cartográfica: terreno real + imagería satelital de **toda la Región de
Coquimbo, Chile** (cuencas de Elqui, Limarí y Choapa más la franja costera)
en un mapa 3D interactivo, con la red hídrica real, quebradas de activación
rara, comunidades clasificadas por riesgo, badenes, sub-cuencas, embalses,
lluvia de las últimas 24h, pronóstico a 7 días, situación de emergencia
reportada por prensa, un asesor IA de alertas (Claude) y una **estimación
gruesa, no calibrada**, de zona inundable simulable a mano o por lluvia.
**No es una herramienta validada para decisiones de evacuación.**

## Ampliación regional (2026-07-20)

Hecha durante el temporal real de julio 2026 (estado de catástrofe decretado
para la región ese mismo día):

- **Cobertura regional completa**: bbox -29.0/-32.35 lat. DEM de 325MB
  (7740x12420px), 5.316 teselas de terreno (z12), HAND regional, 101
  sub-cuencas (Elqui 35, Limarí 37, Choapa 29). Las cuencas costeras menores
  no están delimitadas (el modelo HAND no opera ahí, el resto de capas sí).
- **Red hídrica regional OSM**: 4.855 tramos (420 ríos, 4.080 esteros/
  quebradas, 350 canales) — **3.813 intermitentes (79%)**. 1.743 badenes +
  113 pasos a nivel.
- **Quebradas de activación rara** (`/api/quebradas`, capa naranja
  segmentada): los 3.695 cauces `intermittent=yes` — secos casi siempre,
  se activan con lluvia intensa; su grosor crece con los mm activos.
- **Descargas quebrada→río** (`/api/confluencias`, scripts/10): 557 puntos
  donde una quebrada intermitente desemboca en un cauce permanente
  (validación: `Río Chalinga → Río Choapa` es el desborde real de hoy en
  Salamanca).
- **Comunidades en riesgo** (`/api/places_risk`, scripts/09): 1.141 lugares
  poblados OSM clasificados por cercanía a cauces (242 en riesgo alto —
  Salamanca a 120m del río, Andacollo a 53m de una quebrada). Es un proxy
  geométrico, no un mapa de riesgo oficial — cruzarlo con HAND es Fase 2.
- **Emergencia actual** (`/api/situation`, capa roja): 13 puntos afectados
  del temporal según prensa (evacuaciones SENAPRED, desbordes, daños) con
  fuente por punto y coordenadas aproximadas. Se actualiza manualmente por
  sesión de trabajo — no es un feed automático.
- **Embalses con geometría real OSM** (26, incl. Puclaro/Recoleta/Corrales/
  La Laguna/El Bato). La Paloma quedó parcial y Cogotí ausente por mapeo OSM
  irregular — pendiente.
- El **asesor IA** ahora recibe la situación de prensa, las comunidades en
  riesgo alto y las estadísticas de quebradas; su prompt prioriza la realidad
  reportada sobre el modelo.
- Correcciones de auditoría: repo git iniciado, plantilla `.env.example`
  limpia (rotar la clave de Anthropic si se compartió), lock + rate-limit
  en `/api/ai_analysis`, suite de 12 tests, `requirements.lock.txt`.

## Novedades de esta sesión (2026-07-18, continuación)

- **Dibujar río a mano sobre el terreno 3D** (panel "Dibujar río", `frontend/js/app.js: setupRiverDrawing`): clic sobre el mapa agrega puntos (marcados con puntos azules + línea de vista previa que sigue el cursor), doble clic termina el trazo. El resultado se renderiza como un `CorridorGraphics` con `clampToGround: true`, que se amolda al relieve real (sube y baja siguiendo la malla del terreno) en vez de ser una cinta plana genérica, y **al terminar se abre automáticamente el modal Three.js con el corte de terreno real del DEM** centrado en el punto medio del trazo (mismo modal que al hacer clic en la red de río). Es una capa puramente visual del lado del cliente, no se guarda ni se manda al backend.
  - Gotcha de picking resuelto: `scene.pickPosition` devuelve null sobre terreno sin entidades en muchos entornos (depende del buffer de profundidad) — el orden robusto es `globe.pick(rayo)` → `pickPosition` → `camera.pickEllipsoid` como último fallback (sin relieve, pero el trazo se drapea igual por el clampToGround). Ver `pickTerrainPosition()`.
- **Colores unificados en azul**: el polígono de inundación (antes rojo `#ff3b3b`) ahora se dibuja en azul (`#2f8dff`), igual que la red de río (`#1fa8ff`) y el trazo dibujado a mano, y se agrega después de la capa de río para quedar por encima.
- **Toggle "Mancha de inundación"** (panel "Capas"): permite apagar los polígonos azules de inundación para ver limpio el río dibujado (o el mapa base). El estado se respeta aunque la capa se re-renderice al cambiar nivel/lluvia (bandera `window.__floodVisible` que `renderFlood` consulta al crear cada capa nueva).
- **Botón "Ver relieve 3D y simular crecida"**: reabre en cualquier momento el modal Three.js del corte de terreno centrado en el río dibujado (se habilita al terminar un trazo, se deshabilita con "Borrar"). Hacer clic directamente sobre el corredor dibujado también lo abre, igual que con la red de río.
- **Más detalle de terreno 3D**, en los dos frentes:
  - **Mapa principal**: `MAX_ZOOM` subido de 10 a 12 en `scripts/03_process_terrain.py` (1655 teselas, antes 157). Con z10 el espaciado de muestreo era ~305m — desaprovechaba 10x el DEM de ~30m; con z12 baja a ~76m. Para que fuera viable se reemplazó el muestreo punto a punto (`dataset.sample`, el cuello de botella) por lectura de ventana + interpolación bilineal vectorizada que mantiene los bordes exactos de tesela (sin grietas entre vecinas) — la regeneración completa ahora tarda ~1 minuto.
  - **Modal de corte de terreno**: `GRID_N` subido de 64 a 128 en `backend/terrain_profile.py` (~23m entre muestras, aprovecha todo el DEM) y ahora el relieve se drapea con la **imagería satelital real de Esri** (la misma del mapa principal): `loadSatelliteTexture()` en `app.js` cose las teselas web-mercator del parche en un canvas y lo aplica como textura al mesh de Three.js. Si la imagería no carga (sin red), quedan los colores por elevación de antes. El backend ahora también devuelve `bounds` del parche (con fallback client-side si el server corre código viejo).
- **Zoom en el modal 3D**: rueda del mouse para acercar/alejar la cámara orbital (radio 60–700, `onWheel` en `initRiverScene`).
- **El agua del modal sigue el cauce real** (iterado en dos pasos esta sesión): antes era un plano horizontal — en un valle inclinado se llenaba primero la esquina baja del parche y la inundación parecía avanzar "del mar hacia la cordillera", al revés de una crecida por lluvia. Un primer arreglo usó perfiles por sección transversal recta al eje del valle, pero eso mojaba franjas completas lejos del río. Lo que quedó es un **"HAND local" aproximado**: la base del agua en cada celda es la elevación mínima en una ventana de ~300m a su alrededor (filtro min separable + suavizado 3x3 sobre la grilla de 128x128), es decir, el fondo del valle/quebrada más cercano según el propio DEM. El agua queda a `nivel` metros sobre esa base, así la inundación es una cinta que se pega al trazado real del río (curvas y quebradas tributarias incluidas) y se ensancha cauce abajo al subir el nivel. Como base ≤ terreno en toda celda, con nivel bajo solo se moja la franja del cauce — por eso el % de área inundada ahora muestra un decimal cuando es <10%. Sigue siendo una heurística local, no hidráulica real (no hay conectividad de flujo: una depresión aislada también se moja).
- **Red hídrica real de OSM** (`scripts/08_fetch_osm_hydro.py`, endpoint `/api/waterways`, capa "Ríos y quebradas (OSM)"): 1194 tramos dentro de la cuenca (87 ríos, 842 esteros/quebradas, 265 canales de riego; 353 con nombre), descargados de Overpass y recortados al polígono de la cuenca. Es el trazado colaborativo real — mucho más preciso que la red derivada del DEM, que queda como capa opcional apagada ("Red derivada del DEM") y como insumo del modelo HAND. Estilo por tipo (río grueso azul, quebrada fina celeste, canal verdoso sin engrosado por lluvia). Clic en cualquier tramo abre el modal de corte de terreno con el nombre del cauce en el título.
  - Gotcha Overpass: la clave `ford` consultada por existencia (`node["ford"]`) da 504 sostenido en tres espejos (clave sin índice por valor); por valor exacto (`node["ford"="yes"]`) responde en segundos. Las consultas del script van separadas por eso. El 406 de la sesión 2026-07-18 no se repitió (probable causa: falta de User-Agent identificable, ahora se envía).
- **Badenes y pasos a nivel** (`/api/crossings`, capa "Badenes y pasos a nivel"): 328 puntos OSM dentro de la cuenca — 307 badenes/vados (`ford=yes` en nodos + caminos con `ford`) y 21 pasos a nivel ferroviarios. Son los puntos donde un camino cruza el agua o la vía a nivel: lo primero que se corta en una crecida. Puntos naranja (badén) y gris claro (paso a nivel), escalados por distancia para no saturar el mapa. Cobertura limitada a lo que esté mapeado en OSM — no es un catastro vial oficial.
- **Gotas de lluvia animadas**: (a) en el mapa principal, un canvas 2D superpuesto (`setupRainOverlay`) hace caer gotas con densidad proporcional a los mm activos (lluvia real/simulada/pronóstico; en modo manual no llueve) y también mientras se dibuja un río; (b) en el modal 3D, un `THREE.Points` con 300–2500 gotas (según mm) cayendo sobre el territorio en el render loop.
- **Asesor IA de alertas con Claude** (`backend/ai_advisor.py`, endpoint `/api/ai_analysis`, panel "Asesor IA (Claude)"): botón que genera con la API de Anthropic (modelo `claude-opus-4-8`, configurable con `AI_ADVISOR_MODEL`) una evaluación de riesgo estructurada — nivel de alerta (verde/amarilla/naranja/roja), análisis del clima, **comparación contra registros históricos reales**, zonas en riesgo con nombre, recomendaciones priorizadas y criterio de evacuación preventiva. El contexto que se le entrega al modelo (~3KB, sin geometrías) junta: lluvia real 24h, pronóstico 7 días con acumulados, estimación HAND actual, estadísticas históricas de lluvia, y el inventario de la cuenca (embalses, ríos con nombre, 307 badenes + 21 pasos a nivel). Salida forzada a JSON con `output_config.format` (json_schema) + adaptive thinking. Caché de 15 min en backend (cada análisis es una llamada real con costo). Requiere `ANTHROPIC_API_KEY` en `.env` (ver `.env.example`); sin clave, el endpoint devuelve 503 con instrucciones.
  - **Registros históricos** (`backend/rain_history.py`): ~26 años de precipitación diaria (2000→hoy) del archivo ERA5 de Open-Meteo en Ovalle, reducidos a percentiles (p90/p95/p99 de día lluvioso), promedio anual (127mm — coherente con cuenca semiárida), y top-10 días más lluviosos (el récord de la serie, 91.9mm el 2003-05-21, y el temporal real de mayo 2017 aparecen — buena señal). Cacheado en `data/hydro/rain_history_stats.json`. Es reanálisis de modelo en un punto, no estaciones DGA — sirve para percentiles, no récords oficiales.
  - El prompt obliga al modelo a ser conservador con el nivel de alerta, honesto con la incertidumbre (modelo no calibrado, pronóstico degrada tras día 3-4) y a derivar siempre la decisión real a DGA/SENAPRED/municipio.
- **Grosor dinámico según lluvia**: tanto la red de río real como el contorno del polígono de inundación y el corredor dibujado a mano se engruesan a medida que sube la cantidad de mm de lluvia activa (automática, simulada o pronóstico acumulado) — `rainGrowthWidth()` en `app.js`. En modo manual (slider de nivel, sin mm asociado) se usa el grosor base. Es solo una señal visual, no una escala real de caudal.

## Estado actual (2026-07-18)

**Fase 1 (cartografía base) completa**: cuenca delimitada, DEM descargado,
terreno 3D generado (157 teselas quantized-mesh, self-hosted) y visor
funcionando de punta a punta — imagería satelital real, terreno con relieve
real, marcadores y contorno de la cuenca, todo verificado navegando en el
navegador (vista inclinada mostrando el valle del río con relieve real, no
solo textura plana).

**Estimación de crecida por lluvia, interactiva**: capa de lluvia acumulada
de 24h (Open-Meteo), red de río real (extraída del DEM), sub-cuencas
tributarias, polígonos de embalses, y una mancha de inundación estimada
(método HAND + balance de volumen) que se puede disparar de cuatro formas: (1)
automático según la lluvia real de las últimas 24h, (2) manualmente con un
slider de nivel de agua (m), o (3) escribiendo una cantidad de lluvia a
simular (mm), o (4) eligiendo un día del pronóstico a 7 días (lluvia
acumulada hasta ese día).

**Corte de terreno al hacer clic en el río**: al seleccionar cualquier tramo
de la red de río en el mapa 3D se abre una ventana con el relieve real del
DEM (parche de 3x3km, no sintético) en una escena Three.js separada, con un
plano de agua animable (slider + los mismos 3 presets) que muestra el % de
área inundada localmente y un estado (Normal/Alerta/Crecida mayor). El nivel
inicial se sincroniza con el nivel activo del mapa principal.

**Bug corregido en esta sesión**: subir los mm de lluvia simulada no hacía
subir el nivel de agua de forma visible. La causa real era el umbral que
define "esto es cauce" para el cálculo de HAND (`STREAM_THRESHOLD`), que
estaba tan bajo (500 celdas ≈ 11km² aguas arriba) que casi cualquier gully
contaba como río, dejando gran parte del terreno con HAND≈0 y una "inundación
base" de ~307km² incluso con 0mm de lluvia — subir la lluvia solo movía esa
cifra ~20%, repartido en cientos de parches diminutos, imperceptible en el
mapa. Subir el umbral a 10.000 celdas (≈225km²) bajó la base a 85km² y ahora
300mm mueve el área a 219km² (2.6x), un cambio mucho más visible y realista.
De paso, la red de río quedó mucho más limpia (52 segmentos en vez de 957).
También se corrigió una condición de carrera: respuestas de peticiones viejas
(HTTP) podían llegar después de una más nueva y "pisar" el resultado
correcto en el mapa — se agregó un contador de petición que descarta
cualquier respuesta que ya no sea la más reciente.

Todo esto verificado funcionando de punta a punta con datos reales (6+
capas cargadas en el visor, paneles con números correctos, interacciones de
slider/botones/modal probadas por consola) **pero sin poder confirmar
visualmente con captura de pantalla en esta sesión** — el panel de vista
previa del navegador dejó de responder a capturas tras muchas recargas
seguidas (agotamiento de contextos WebGL del entorno de prueba, no un bug
del proyecto — ya se había logrado una captura exitosa antes en la misma
sesión). Recomiendo abrirlo tú mismo (`uvicorn backend.app:app --port 8000`
→ `localhost:8000`) para confirmar visualmente.

El bloqueo de Visual C++ Build Tools (`vswhere` no registraba la instalación)
se resolvió reiniciando Windows y agregando el directorio del instalador de
Visual Studio al PATH de usuario (`vcvarsall.bat` invoca internamente
`vswhere.exe` sin ruta completa, y si no está en PATH corrompe el parseo de
variables de entorno que hace `setuptools`).

## Clima en vivo drapeado en 3D (2026-07-22)

Nueva sección **"Clima en vivo (satelital)"** en el panel principal, con
dos overlays al estilo Meteored (referencia:
[meteored.cl/mapas-meteorologicos/prcnub-ac.html](https://www.meteored.cl/mapas-meteorologicos/prcnub-ac.html)).
Ambos se drapean sobre el terreno 3D — siguen el relieve y la cámara.

- **Nubes satelitales (IR)** — infrarrojo termal del satélite NOAA
  GOES-East ABI Band13, servido por NASA GIBS. Es la misma vista base de
  nubes que usan Meteored y los reportes operacionales, funciona día y
  noche. Frames cada 10min con ~30min de latencia, slider de tiempo para
  las últimas 2h. Sin API key.
- **Viento animado (partículas)** — 900 partículas de trazo con estelas
  que decaen, estilo Meteored/earth.nullschool. Interpolación bilineal
  sobre una grilla 15×15 de viento a 10m servida por `/api/wind_grid`
  (Open-Meteo, cache 30min). Color por velocidad (azul→cian→amarillo→rojo)
  y velocidad de advección adaptativa al zoom actual. Sigue la proyección
  3D de Cesium: al inclinar la cámara las estelas siguen la esfera.
  **Sincronizado con el slider satelital**: el backend devuelve serie
  horaria (past 3h + forecast 1h) y al mover el slider satelital las
  partículas cambian al frame de viento más cercano en el tiempo — se ve
  cómo cambió el patrón mientras avanzaba el frente.

## Relaves mineros y simulación de derrame (2026-07-22)

Nueva capa **☣️ Relaves mineros** (bajo demanda) que muestra los **406
depósitos de relaves de la Región de Coquimbo** — la mayor concentración
de todo Chile. Fuente: catastro oficial de SERNAGEOMIN vía el Feature
Server ArcGIS de la SMA (2018, coordenadas exactas). Descargados y
enriquecidos por [scripts/11_fetch_relaves.py](scripts/11_fetch_relaves.py)
con: distancia al río permanente más cercano, nombre del cauce,
polilínea aguas abajo (~8km) siguiendo el río real, elevación del DEM y
un riesgo geométrico simple (alto/medio/bajo según cercanía al cauce).

Cada relave se muestra como un punto coloreado por estado dinámico:
- **Gris (dormido)** — condiciones normales
- **Amarillo (en riesgo)** — crecida incipiente o lluvia significativa
- **Rojo (vertiendo)** — trigger de derrame activado

**Trigger de derrame** (mismas reglas en frontend y en el chatbot):
- `riesgo_alto Y delta_h > 2m` → vierte
- `riesgo_alto|medio Y lluvia > 80mm/24h` → vierte
- `delta_h > 6m` (crecida mayor) → todos los cercanos al río vierten
- Estados intermedios se marcan como "en riesgo"

Cuando un relave vierte, un **canvas overlay** dispara una **pluma de
partículas** con arrastre visualmente rico — mismo mecanismo de trazo
con estela que las partículas de viento, con tres refinamientos que dan
la sensación de "el río se lleva el relave":

- **Densidad**: 120 partículas por relave vertiendo (309 vertiendo en
  crecida mayor = ~37k trazos/segundo, canvas 2D lo aguanta cómodo).
- **Jitter lateral**: cada partícula tiene un offset perpendicular al
  segmento (±45m) atenuado cerca del origen, así salen del punto exacto
  del relave y se abren corriente abajo como una pluma real (no una
  línea de 1px).
- **Gradiente de color**: naranja intenso (recién liberado, cerca del
  relave) → marrón óxido (disperso, corriente abajo). Simula
  dilución/dispersión del material.
- **Velocidad proporcional a la crecida**: la advección se acelera 0.6x
  a 3x según `max(delta_h/3, rain/80)` — río más grande, corriente más
  rápida arrastrando material.

Se re-evalúa automáticamente cuando el usuario mueve el slider de nivel,
simula lluvia o cambia el día del pronóstico.

El chatbot conversacional expone la nueva herramienta `evaluar_relaves(delta_h_m, rainfall_mm)` — probá con *"¿qué pasa con los relaves si el río sube 4 metros?"* o *"¿qué relaves están en peligro con 100mm de lluvia?"* y responderá con conteos y los sitios críticos más cercanos al cauce.

**Descargo importante**: los datos son del catastro público oficial pero
NO reemplazan las notificaciones DS 248 del operador minero. La
detección de derrame es un modelo simplificado — no es una evaluación de
estabilidad hidráulica. Cruzar con inspecciones oficiales antes de usar
para cualquier decisión.

## Arranque limpio (2026-07-22)

Todas las capas del panel arrancan **desactivadas** para que el usuario
descubra el visor eligiendo lo que quiere ver, en vez de aterrizar sobre
un mapa cargado con seis capas apiladas. Los data sources se cargan una
sola vez (o bajo demanda para las lazy) pero quedan ocultos hasta que el
checkbox se marque.

Slider de opacidad (0-100%) compartido y auto-refresh cada 5min. Los tiles
son PNG con transparencia y Cesium los drapea automáticamente sobre el
DEM — al inclinar la cámara los frentes siguen la topografía. Implementado
en `initWeatherOverlays()` de [frontend/js/app.js](frontend/js/app.js).

## Chatbot conversacional (2026-07-22)

Panel flotante abajo a la derecha ("💬 Preguntar al asistente"): habla con
Claude en modo *tool use*. El modelo elige qué herramienta llamar según la
pregunta y el visor ejecuta las acciones resultantes en vivo.

Ejemplos que funcionan:
- *"¿Qué pasa si llueven 80mm en 24h?"* → corre `simular_lluvia(80)`, actualiza
  la mancha de inundación y explica el resultado.
- *"Volá a Salamanca y mostrame el corte del río"* → `buscar_lugar` resuelve
  las coordenadas, la cámara vuela y se abre el modal 3D de corte de terreno.
- *"Simulá el peor escenario del pronóstico de 7 días"* → identifica el día
  con más lluvia acumulada y aplica `pronostico_dia(dia)`.
- *"¿Cuál es la situación actual de emergencia?"* → devuelve
  `situacion_actual()` con los puntos afectados reportados por prensa.

Herramientas expuestas al modelo: `buscar_lugar`, `simular_lluvia`,
`simular_nivel`, `pronostico_dia`, `ver_terreno_en`, `situacion_actual`,
`lluvia_actual`, `pronostico_7d`. El backend traduce cada `tool_use` a una
acción de UI (`fly_to`, `set_water_slider`, `set_rain_sim`, `set_forecast_day`,
`open_terrain_profile`) que el frontend ejecuta antes de renderizar la
respuesta, para que el mapa se mueva mientras se lee el texto.

Requisitos: `ANTHROPIC_API_KEY` en `.env`. Rate limit por IP: 8 preguntas /
60s. Historial mantenido solo en el cliente y enviado en cada turno (hasta 20
turnos). El endpoint es `POST /api/chat` — implementación en
[backend/chat.py](backend/chat.py).

## Despliegue en AWS (opcional)

Guía completa en [docs/AWS_DEPLOYMENT.md](docs/AWS_DEPLOYMENT.md):
arquitectura (S3+CloudFront para frontend + terreno, App Runner o EC2 para
FastAPI, Secrets Manager para la API key), pasos concretos con AWS CLI y
estimación de costos (~USD 15-40/mes con acceso público moderado).

## Documentos

- [docs/TECNICO_DETALLADO.md](docs/TECNICO_DETALLADO.md) — documentación
  técnica completa: arquitectura, pipeline de datos, endpoints, decisiones
  y gotchas resueltos (para retomar el código).
- [docs/HISTORIAL_CHAT.md](docs/HISTORIAL_CHAT.md) — historial de las
  sesiones de chat que construyeron el proyecto, con prompts para retomar
  en un chat nuevo.
- [docs/AUDITORIA.md](docs/AUDITORIA.md) — auditoría completa del proyecto
  (hallazgos priorizados: seguridad, robustez, rendimiento, deuda técnica).
- [docs/INFORME_TECNICO.md](docs/INFORME_TECNICO.md) — informe ejecutivo:
  capacidades, limitaciones honestas, contexto de riesgo regional y usos
  futuros (de educación a sistema de alerta temprana).

## Optimización de rendimiento (2026-07-21)

Sesión dedicada a bajar la latencia del visor. Cambios medidos en el navegador:

| Recurso | Antes | Después | Factor |
|---|---:|---:|---:|
| `/api/waterways` | 17 MB / 4855 tramos | **287 KB / 805 tramos** | 60x |
| `/api/quebradas` | 12 MB / 3695 tramos | **582 KB / 1989 tramos** | 21x |
| `/api/flood?delta_h=N` (repetido) | 5.4 s | **14 ms** | 385x |
| DataSources al inicio | 12 | **6** | — |
| Capas de puntos (crossings, places, confluencias) | Entity individuales | **PointPrimitiveCollection** | 10-100x en render |

Qué se hizo:

- **`scripts/11_simplify_layers.py`**: pre-simplifica ríos y quebradas con
  Douglas-Peucker (~150m ríos, ~200m quebradas) y filtra las quebradas muy
  cortas sin nombre. Genera `.simple.geojson` que el backend sirve por
  defecto; el crudo queda para análisis (`?full=true`).
- **Caché LRU de la mancha de inundación** en `backend/flood.py` por `delta_h`
  redondeado a 0.1 m — arrastrar el slider ya no vuelve a rasterizar HAND.
- **Lazy load**: la carga inicial solo trae 4 capas (ríos permanentes,
  comunidades, emergencia actual, embalses) + la mancha. Quebradas,
  confluencias, badenes, red DEM y sub-cuencas se cargan **solo al activar
  su toggle** (marcadas "bajo demanda" en el panel).
- **`PointPrimitiveCollection` + `LabelCollection`** nativas para las tres
  capas de puntos (1141 comunidades, 1856 badenes, 557 confluencias). Batch
  único en GPU en vez de 3554 Entities.

## Iniciar la aplicación (launcher)

Doble clic en **`iniciar.bat`** (Windows), o `iniciar.ps1` (PowerShell) o
`./iniciar.sh` (macOS/Linux). El launcher verifica que existan `.venv/`, el
DEM/HAND generado y que el puerto 8000 esté libre; después arranca uvicorn
y abre el navegador automáticamente en `http://localhost:8000`. `Ctrl+C` en
la ventana lo detiene.

Si prefieres el arranque manual, ver debajo.

## Cómo correr el proyecto

```bash
# 1. Activar el entorno virtual (ya creado en .venv)
.venv\Scripts\activate          # PowerShell / cmd
# 2. Levantar el backend (sirve el visor + terreno + datos)
uvicorn backend.app:app --reload --port 8000
# 3. Abrir en el navegador
http://localhost:8000
```

Si necesitas regenerar los datos desde cero, corre en orden:

```bash
python scripts/01_delimit_basin.py    # delimita la cuenca real (HydroBASINS)
python scripts/02_download_dem.py     # descarga el DEM (requiere OPENTOPOGRAPHY_API_KEY en .env)
python scripts/03_process_terrain.py  # genera las teselas de terreno 3D
python scripts/05_compute_hand.py     # calcula HAND + extrae la red de río real
python scripts/06_fetch_reservoirs.py # genera polígonos (ilustrativos) de los embalses
python scripts/07_delimit_subbasins.py # sub-cuencas tributarias (HydroBASINS nivel 9)
python scripts/08_fetch_osm_hydro.py  # red hídrica OSM + badenes y pasos a nivel
python scripts/09_fetch_places_risk.py # comunidades + clasificación de riesgo
python scripts/10_analyze_quebradas.py # quebradas intermitentes + confluencias
python scripts/11_simplify_layers.py  # versiones simplificadas para el visor
```

La lluvia, el pronóstico y la mancha de inundación (`/api/rainfall`,
`/api/forecast`, `/api/flood`) se calculan en vivo en cada carga del visor
(con caché de 15 min en el backend) — no requieren un script aparte, solo
que `05_compute_hand.py` se haya corrido una vez.

## Fuentes de datos usadas

| Dato | Fuente | Fecha de descarga |
|---|---|---|
| Elevación (DEM) | Copernicus GLO-30 vía [OpenTopography API](https://portal.opentopography.org) | 2026-07-18 |
| Imagería satelital | [Esri World Imagery](https://www.arcgis.com/home/item.html?id=974d45be315c4c87b2ac32be59af9a0b) (tiles públicos, sin API key) | en vivo (no se descarga, se sirve directo desde el navegador) |
| Delimitación de cuenca | [HydroBASINS](https://www.hydrosheds.org/products/hydrobasins) (hydrosheds.org), nivel Pfafstetter 7 | 2026-07-18 |

## Bounding box

El bbox propuesto originalmente se validó contra HydroBASINS y se ajustó:

| | Norte | Sur | Este | Oeste |
|---|---|---|---|---|
| Original | -30.15 | -31.25 | -70.00 | -71.70 |
| Ajustado (usado) | -30.21 | -31.43 | -70.18 | -71.70 |

Área de la cuenca calculada: **11.687 km²** (la cifra oficial de la DGA es
~11.696 km², diferencia <0.1% — buena señal de que la delimitación es
razonable, pero sigue siendo una aproximación por HydroBASINS, no el
polígono oficial chileno).

## Supuestos y datos no verificados — REVISAR antes de Fase 2

Todo lo siguiente está marcado también en el código fuente donde corresponde:

- **Coordenadas de Ovalle y los tres embalses** (`data/markers.json`): de
  memoria, no verificadas contra fuente oficial (DGA, municipalidad,
  operador de los embalses). Marcadas con `verificado: false` (aparecen con
  `*` en el visor).
- **Estaciones fluviométricas/pluviométricas de la DGA**: no incluidas —
  no tengo sus coordenadas verificadas. Pendiente obtenerlas de
  snia.mop.gob.cl / bna.dga.cl.
- **Punto ancla y nivel Pfafstetter para delimitar la cuenca**
  (`scripts/01_delimit_basin.py`): se usó Ovalle como punto ancla y nivel 7;
  no se confirmó visualmente contra un shapefile oficial de la DGA/IDE Chile.
- **Datum vertical del terreno** (`scripts/03_process_terrain.py`): el DEM
  usa el geoide EGM2008, Cesium espera altura elipsoidal WGS84. No se
  corrigió (diferencia aprox. 30-40m en esta zona). El relieve relativo es
  correcto; la altura absoluta puede estar desplazada.
- **Resolución del terreno**: limitada a zoom nivel 10 (`MAX_ZOOM` en
  `03_process_terrain.py`) para priorizar fluidez de navegación. Se puede
  subir para más detalle una vez validado el rendimiento.

## Estimación de crecida por lluvia — metodología y limitaciones

**Esto NO es un modelo hidrológico calibrado. Es una ilustración rápida de
"dónde podría subir el agua", no una predicción.** Léelo antes de mostrarle
esto a alguien como si fuera confiable:

1. **Lluvia**: se consulta Open-Meteo (`backend/rainfall.py`) en una grilla
   de 36 puntos sobre la cuenca, se suma la precipitación horaria de las
   últimas 24h por punto y se promedia. Open-Meteo entrega salida de modelo
   meteorológico (ICON/ECMWF/GFS "best match"), **no medición satelital ni
   de estaciones en tierra**, resolución ~9-25km — mucho más gruesa que la
   cuenca real.
2. **Escorrentía**: lluvia promedio × `RUNOFF_COEFFICIENT = 0.3`
   (`backend/flood.py`) — un coeficiente fijo inventado como punto de
   partida razonable para una cuenca semiárida, **no calibrado con datos
   reales**. El coeficiente real varía muchísimo según humedad previa del
   suelo, cobertura vegetal e intensidad de la tormenta.
3. **HAND (Height Above Nearest Drainage)**: `scripts/05_compute_hand.py`
   usa `pysheds` para calcular, desde el DEM reproyectado a UTM 19S y
   remuestreado a 150m, cuánto tiene que subir el agua en cada celda para
   conectarse con la red de drenaje (derivada del propio DEM por
   acumulación de flujo, con un umbral arbitrario — no se validó contra el
   cauce real del río).
4. **Nivel estimado**: se busca (bisección) el alza `delta_h` tal que el
   volumen "contenido" bajo HAND≤delta_h iguale el volumen de escorrentía
   estimado. La mancha de inundación son las celdas con HAND≤delta_h.

El visor tiene cuatro modos, todos sobre el mismo HAND real de la cuenca:

- **Automático** (botón "Actualizar según lluvia actual"): el `delta_h` de
  arriba, derivado de la lluvia real de las últimas 24h.
- **Manual** (slider + presets "Caudal bajo" 0.5m / "Alerta amarilla" 2m /
  "Crecida mayor" 6m — `GET /api/flood?delta_h=X`, sin llamar a Open-Meteo):
  fijás vos el alza de nivel y ves qué zona quedaría inundada según el
  terreno real. Los valores de los presets son ilustrativos, no umbrales
  oficiales de alerta de la DGA.
- **Simular lluvia** (slider 0-300mm — `GET /api/flood?rainfall_mm=X`):
  escribís cuánta lluvia caería y el sistema corre el mismo modelo
  escorrentía→HAND que el modo automático, pero con el valor que vos elijas
  en vez de la lluvia real medida.
- **Pronóstico 7 días** (botones por día — `GET /api/flood/forecast_day?day=N`):
  usa el pronóstico diario de Open-Meteo (`backend/rainfall.py:fetch_forecast_7d`)
  y muestra la inundación estimada para la lluvia **acumulada desde hoy
  hasta ese día** (progresivo, no cada día por separado) — responde "si
  sigue lloviendo así, ¿cómo se ve el río el jueves?". La incertidumbre del
  pronóstico crece mucho día a día, sobre todo después de 3-4 días; tratar
  los últimos días de la semana como muy ilustrativos.

Desde el panel ("Capas") se pueden activar/desactivar: red de río,
sub-cuencas, embalses y puntos de lluvia — ver metodología de cada una en
"Río, sub-cuencas y embalses" más abajo.

Lo que esto **ignora por completo**: el tránsito de la crecida en el tiempo
(asume equilibrio instantáneo con toda la lluvia de 24h cayendo a la vez),
infiltración variable, capacidad real del cauce, los tres embalses (que
retienen agua y cambian todo el comportamiento aguas abajo), y cualquier
calibración contra caudales históricos del Limarí. Los polígonos además se
simplifican fuertemente (tolerancia 250m, se descartan fragmentos <10
celdas) porque la rasterización a 150m generaba decenas de miles de
vértices que colgaban el render de Cesium — esto reduce aún más la
precisión aparente, lo cual es intencional (mejor que sugiera menos
precisión de la que tiene).

## Río, sub-cuencas y embalses

- **Red de río** (`/api/river`, capa azul, clickeable): NO es un trazado
  oficial ni de OpenStreetMap — se extrae directamente del DEM con `pysheds`
  (`grid.extract_river_network`), a partir de la acumulación de flujo D8
  sobre el raster de 150m. 52 segmentos (`STREAM_THRESHOLD = 10000` celdas
  ≈ 225km² aguas arriba — subido desde 500 en esta sesión, ver "Estado
  actual"). Sigue siendo un umbral arbitrario, no calibrado contra el cauce
  real — puede haber tramos que en la realidad son cauce y acá no aparecen,
  o viceversa en zonas planas.
- **Sub-cuencas** (`/api/subbasins`, capa morada, oculta por defecto): 37
  polígonos de HydroBASINS nivel Pfafstetter 9 dentro de la misma cuenca del
  Limarí ya delimitada en Fase 1. **No están identificadas por nombre** (no
  hay forma de saber cuál corresponde al Río Grande, Hurtado, Rapel/Cogotí o
  Guatulame sin cruzarlas a mano contra un mapa oficial con nombres — no se
  hizo). Son la subdivisión automática de HydroBASINS, no cuencas
  tributarias "oficiales".
- **Embalses** (`/api/reservoirs`, polígonos azules): se intentó obtener la
  geometría real desde OpenStreetMap (Overpass API) y **falló en los dos
  servidores probados** (`overpass-api.de` con error 406, `overpass.kumi.systems`
  con una respuesta de error distinta) — no se pudo diagnosticar más a fondo
  en esta sesión, podría ser un bloqueo del entorno de red hacia Overpass o
  un problema temporal del servicio. El fallback (`scripts/06_fetch_reservoirs.py`)
  dibuja un **círculo ilustrativo** centrado en la coordenada del embalse,
  con radio calculado desde una superficie aproximada **de memoria, no
  verificada** (La Paloma ~7.5km², Recoleta ~1km², Cogotí ~1.5km²) — no es
  la forma real del embalse ni refleja su nivel de llenado actual. Si
  Overpass vuelve a andar, el script queda con un TODO marcado para
  implementar el parseo real.

## Corte de terreno al seleccionar el río

Al hacer clic en cualquier tramo de la capa de río se abre un modal
(`frontend/js/app.js: openRiverModal`) con una escena Three.js separada del
mapa Cesium principal, mostrando el relieve real del DEM alrededor del punto
clickeado (no una maqueta genérica como el mockup original que sirvió de
referencia visual):

- Backend (`backend/terrain_profile.py`, endpoint `/api/terrain_profile`):
  recorta una grilla de 64x64 muestras del DEM original (~30m, no el de
  150m usado para HAND) en un parche de 3x3km centrado en el punto
  clickeado.
- La "altura del cauce" de referencia es la elevación **mínima dentro del
  parche** — un supuesto razonable si el parche está cerca del río, pero es
  independiente del HAND real de la cuenca (por eso el % de inundación que
  muestra este modal es una estimación *local*, distinta de la del panel
  principal — puede no coincidir con el delta_h global).
- El slider/presets del modal controlan un plano de agua que sube sobre el
  terreno real, con el mismo cálculo simple de "% de celdas del parche por
  debajo del nivel de agua" que usa el mockup original.
- El nivel inicial se sincroniza con el delta_h activo del panel principal.

## Qué falta para la Fase 2 / 3

- **Calibrar o reemplazar** el coeficiente de escorrentía y el umbral de
  cauce contra caudales históricos reales del Limarí (DGA).
- Reemplazar Open-Meteo por estaciones pluviométricas reales de la DGA si
  se encuentra una API viable, o por GPM/IMERG (satelital, requiere cuenta
  NASA Earthdata — ver notas de investigación de esta sesión).
- Modelo de tránsito de crecida (no solo balance de volumen instantáneo).
- Incorporar los embalses (La Paloma, Recoleta, Cogotí) como elementos que
  retienen agua y laminan la crecida, no solo geometría visual.
- Reintentar Overpass API para la geometría real de los embalses (ver
  limitación arriba), o conseguir los polígonos oficiales de otra fuente.
- Identificar las sub-cuencas por nombre de tributario real.
- Corrección de datum vertical (geoide → elipsoide) si se requiere precisión
  de cota real en el terreno 3D.
- Reemplazar la delimitación por HydroBASINS con el shapefile oficial de la
  cuenca (DGA/IDE Chile) si se consigue.
- Coordenadas verificadas de embalses, estaciones DGA y quizás vías de
  evacuación.
- El modal de corte de terreno usa una heurística local simple ("% bajo el
  nivel de agua en el parche"), no el HAND real de la cuenca — conectarlo al
  mismo modelo del panel principal para que ambos números sean consistentes.

## Estructura del proyecto

```
data/
  dem/            DEM crudo descargado (GeoTIFF)
  hydro/          Cuenca (GeoJSON), bbox ajustado, sub-cuencas, embalses
  terrain_tiles/  Terreno quantized-mesh generado (self-hosted, sin Cesium ion)
  flood/          DEM reproyectado UTM + raster HAND + red de río real
                  (scripts/05_compute_hand.py)
  markers.json    Ovalle (embalses ahora vienen de hydro/reservoirs.geojson)
scripts/          Pipeline de descarga/procesamiento (correr en orden 01→03,
                  05, 06, 07 — ver "Cómo correr el proyecto")
backend/          FastAPI: terreno, cuenca, sub-cuencas, embalses, red de río,
                  marcadores, lluvia + pronóstico (rainfall.py), inundación
                  estimada (flood.py), corte de terreno local
                  (terrain_profile.py) y el frontend
frontend/         Visor 3D (CesiumJS vía CDN, sin cuenta/token de Cesium ion)
```

## Notas técnicas

- El terreno 3D se generó con un pipeline 100% propio en Python
  (`pymartini` + `quantized-mesh-encoder`), sin depender de Cesium ion.
  Requirió instalar Visual C++ Build Tools (compilador C++) para las
  extensiones nativas de esas librerías.
- La imagería satelital de Esri se consume en vivo desde el navegador
  (no se guarda copia local); requiere conexión a internet para navegar
  el visor, igual que Google Maps.
- Dos gotchas no obvios resueltos en `scripts/03_process_terrain.py` /
  `frontend/js/app.js` si se toca este código:
  - El eje Y de `"available"` en `layer.json` usa la convención nativa de
    Cesium (0 = norte), aunque los archivos `.terrain` en disco usan Y estilo
    TMS (0 = sur) — son ejes opuestos, hay que invertir uno al escribir el
    otro.
  - El frontend verifica si el terreno existe con `fetch(...)` normal (GET),
    no `HEAD` — FastAPI no expone `HEAD` automáticamente en rutas `@app.get`,
    y `CesiumTerrainProvider.fromUrl` queda en un estado que rompe el
    render del globo completo si se le apunta a un `layer.json` inexistente.
  - Después de editar `frontend/js/app.js` o `index.html`, el navegador a
    veces cachea agresivamente incluso con hard-reload — si los cambios no
    se reflejan, navegar a una URL con un query param nuevo (`?cb=123`)
    fuerza la recarga real.
- `pysheds` 0.5 (`scripts/05_compute_hand.py`) usa la función `np.in1d`,
  removida en numpy>=2.0 — se aplica `np.in1d = np.isin` como shim antes de
  importar pysheds. Instalar pysheds también downgradeó numpy a 2.4.6
  (desde 2.5.1) por sus propios requisitos; no debería romper nada más en
  el proyecto pero queda anotado por si acaso.
