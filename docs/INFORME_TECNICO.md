# Informe técnico — Simulador de crecidas del río Limarí

Fecha: 2026-07-20 · Estado: prototipo funcional (Fase 1 completa + extensiones)

---

## 1. Qué es

Visor 3D interactivo de la cuenca del río Limarí (Región de Coquimbo, Chile,
~11.700 km²) que combina terreno real, datos hidrometeorológicos en vivo y un
modelo simplificado de inundación para **ilustrar dónde podría subir el agua
ante una lluvia dada**, con un asesor de alertas basado en IA (Claude) que
sintetiza todo el contexto en una evaluación de riesgo accionable.

**No es** una herramienta validada para decisiones de evacuación reales — es
un prototipo educativo/exploratorio con un camino claro hacia algo operativo
(sección 6).

## 2. Arquitectura

```
┌─ Fuentes externas ─────────────────────────────────────────┐
│ OpenTopography (DEM 30m) · HydroBASINS (cuencas)           │
│ OpenStreetMap/Overpass (ríos, badenes) · Esri (imagería)   │
│ Open-Meteo (lluvia 24h, pronóstico 7d, histórico 26 años)  │
│ API Anthropic (claude-opus-4-8, asesor de alertas)         │
└──────────────┬─────────────────────────────────────────────┘
               │ scripts/01→08 (pipeline reproducible, caché local)
               ▼
┌─ data/ ────────────────────────────────────────────────────┐
│ DEM GeoTIFF · 1655 teselas quantized-mesh (z0–z12)         │
│ HAND 150m UTM · red de ríos (OSM 1194 tramos + DEM 52)     │
│ 328 badenes/pasos a nivel · subcuencas · embalses          │
│ estadísticas históricas de lluvia (ERA5 2000–2026)         │
└──────────────┬─────────────────────────────────────────────┘
               ▼
┌─ backend/ (FastAPI, Python) ───────────────────────────────┐
│ terreno · capas geojson · lluvia/pronóstico (caché 15min)  │
│ inundación HAND (bisección volumen↔nivel) · corte de       │
│ terreno local 128×128 · asesor IA (JSON estructurado)      │
└──────────────┬─────────────────────────────────────────────┘
               ▼
┌─ frontend/ (CesiumJS + Three.js, sin build) ───────────────┐
│ globo 3D con terreno real · 8 capas conmutables · dibujo   │
│ de río sobre relieve · modal 3D con imagería satelital y   │
│ agua que sigue el cauce · lluvia animada · panel IA        │
└────────────────────────────────────────────────────────────┘
```

Decisiones estructurales: 100% self-hosted (sin cuentas de pago obligatorias:
Cesium ion no se usa, el terreno se genera localmente), frontend sin framework
ni build step, pipeline de datos regenerable de cero en menos de una hora.

## 3. Capacidades actuales

| Capacidad | Método | Fidelidad |
|---|---|---|
| Terreno 3D | Copernicus GLO-30 → quantized-mesh z12 (~76m de muestreo) | Alta (relieve relativo) |
| Red hídrica | OSM colaborativo: 87 ríos, 842 quebradas, 265 canales, 353 con nombre | Alta (trazado), sin jerarquía de caudal |
| Puntos críticos | 307 badenes + 21 pasos a nivel (OSM) | Cobertura = lo mapeado en OSM |
| Lluvia actual | Open-Meteo, grilla 36 puntos, promedio de cuenca | Media (modelo, no estaciones) |
| Pronóstico | Open-Meteo 7 días, acumulados progresivos | Media, degrada tras día 3–4 |
| Histórico | ERA5 26 años diarios, percentiles p90/p95/p99, top-10 récords | Media (reanálisis, 1 punto) |
| Inundación | HAND 150m + balance de volumen, escorrentía fija 0.3 | **Baja — no calibrado** |
| Corte local 3D | Parche 3×3km del DEM a 128×128 con imagería drapeada | Alta (relieve), agua heurística |
| Asesor IA | Contexto ~3KB → Claude con salida JSON estructurada | Tan bueno como sus datos de entrada |

Interacciones: simulación por lluvia real / manual / mm simulados / día de
pronóstico; dibujo de río a mano que se modela sobre el relieve; clic en
cualquier cauce → ventana 3D con simulación local de crecida; análisis IA con
nivel de alerta, zonas en riesgo y criterio de evacuación preventiva.

## 4. Limitaciones honestas (las que definen qué NO se puede hacer hoy)

1. **El modelo hidrológico no está calibrado**: coeficiente de escorrentía
   inventado (0.3), equilibrio instantáneo (sin tránsito de onda), sin
   infiltración variable, sin nieve, y **sin los tres embalses** (La Paloma,
   Recoleta, Cogotí) que en la realidad laminan cualquier crecida del sistema.
   El área inundable resultante es una cota superior ilustrativa.
2. **La lluvia no viene de sensores**: Open-Meteo entrega salida de modelo
   (~9–25km de resolución), no estaciones DGA ni radar. En una cuenca de
   montaña la lluvia real puede ser muy distinta celda a celda.
3. **Datum vertical desplazado ~30–40m** (EGM2008 vs elipsoide WGS84): el
   relieve relativo es correcto, las cotas absolutas no.
4. **El histórico es reanálisis en un punto** (Ovalle): sirve para percentiles
   y contexto, no para récords oficiales por estación.
5. **Sin autenticación ni hardening**: pensado para correr en localhost.

## 5. Contexto: por qué esto importa aquí

La Región de Coquimbo es el caso de estudio perfecto de riesgo hidrológico
"seco": una cuenca semiárida que pasa años en sequía extrema y luego recibe
sistemas frontales o ríos atmosféricos que descargan la lluvia de un año en
48 horas. Los antecedentes recientes lo demuestran — y varios aparecen en los
propios datos del proyecto:

- **Mayo 2017**: temporal en Coquimbo/Atacama con evacuaciones e inundaciones
  en el Limarí (el 11-05-2017 figura como 2° día más lluvioso de la serie
  ERA5 del proyecto, 69.6mm).
- **Marzo 2015**: aluviones catastróficos en Atacama (al norte de esta cuenca)
  — el ejemplo canónico de lluvia cálida sobre suelo árido sin capacidad de
  absorción.
- **Julio 2026 (durante el desarrollo)**: el propio sistema registró ~73mm en
  24h — sobre el p99 histórico — y el asesor IA generó alerta roja coherente.

A esto se suman tendencias que aumentan el valor de herramientas así: la
megasequía multidecadal (que endurece suelos y reduce cobertura vegetal →
más escorrentía cuando al fin llueve), el ascenso de la isoterma 0°C (lluvias
que caen donde antes nevaba → cuencas de respuesta más rápida) y la presión
urbana sobre terrazas fluviales en Ovalle y localidades menores.

## 6. Usos futuros

### 6.1 Corto plazo (con lo que ya existe)

- **Educación y difusión del riesgo**: mostrar a comunidades escolares,
  juntas de vecinos y municipios *dónde vive el agua* — el efecto de ver la
  propia casa junto a una quebrada que se pinta de azul con 100mm simulados
  es más elocuente que cualquier charla. El modo "dibujar río + ver relieve"
  sirve para explicar por qué un badén se corta.
- **Tablero de vigilancia informal**: la combinación lluvia-real + pronóstico
  + percentiles históricos + asesor IA ya responde la pregunta útil "¿lo que
  viene esta semana es normal o excepcional para esta cuenca?" con datos, no
  intuición.
- **Inventario de puntos de corte**: los 307 badenes georreferenciados son,
  por sí solos, un insumo para planes municipales de emergencia (qué rutas se
  cortan primero, rutas alternativas).

### 6.2 Mediano plazo (Fase 2–3, requiere trabajo dirigido)

- **Sistema de alerta temprana local (SATL) de bajo costo**: calibrando el
  modelo con caudales históricos DGA e incorporando los embalses, el sistema
  podría pasar de "ilustrativo" a "indicativo": umbrales de lluvia
  pronosticada que disparan avisos automáticos (el asesor IA ya produce la
  pieza de comunicación). Municipios pequeños no tienen acceso a modelación
  hidráulica comercial; esto corre en un PC.
- **Planificación territorial**: mapas de "zona que se moja con nivel X"
  como insumo grueso para revisar permisos de edificación en terrazas
  fluviales, ubicar infraestructura crítica y priorizar defensas ribereñas.
- **Priorización de inversión vial**: cruzar los badenes con la frecuencia de
  activación de sus quebradas (derivable del modelo + histórico) para decidir
  cuáles convertir en puentes primero.
- **Evacuaciones preventivas ensayadas**: usar el simulador para diseñar y
  ensayar rutas de evacuación por escenario (50/100/200mm), antes de la
  emergencia y no durante.

### 6.3 Largo plazo (visión)

- **Replicabilidad**: nada del pipeline es específico del Limarí — cambiar el
  bbox y el punto ancla regenera todo para cualquier cuenca de Chile u otro
  país con los mismos datos públicos globales (Copernicus, HydroBASINS, OSM,
  Open-Meteo). El costo marginal de cubrir las cuencas de Choapa y Elqui es
  cercano a cero.
- **Asimilación de datos reales**: estaciones DGA en línea, radar
  meteorológico si existiera, sensores de nivel de bajo costo (LoRa) en
  badenes críticos — el backend ya tiene la estructura para ingerirlos.
- **IA como capa de síntesis multi-cuenca**: un asesor que vigile N cuencas y
  priorice la atención humana ("de tus 12 cuencas, hoy mira estas dos") es la
  extensión natural del panel actual.
- **Gemelo digital regional**: la combinación terreno + hidrología + puntos
  críticos + histórico es la base de un gemelo digital de riesgo climático
  regional (incendio, sequía y remoción en masa comparten el 80% de esta
  infraestructura de datos).

### 6.4 Qué se necesita para cruzar de prototipo a operativo

| Requisito | Estado | Brecha |
|---|---|---|
| Calibración con caudales DGA | ✗ | Conseguir series (snia.mop.gob.cl), ajustar escorrentía y umbral de cauce |
| Embalses como laminadores | ✗ | Reglas de operación + niveles actuales (o al menos capacidad) |
| Lluvia observada (no solo modelo) | ✗ | Estaciones DGA en línea o GPM/IMERG satelital |
| Tránsito de crecida | ✗ | Modelo hidráulico simple (onda cinemática) o acoplar HEC-RAS |
| Validación institucional | ✗ | Contraste con mapas de inundación oficiales si existen |
| Hardening (auth, HTTPS, tests) | ✗ | Ver AUDITORIA.md |
| Corrección de datum vertical | ✗ | Aplicar EGM2008→WGS84 en el pipeline |

**El principio rector se mantiene**: mientras esas brechas existan, cada
pantalla lo dice — *"NO usar para decisiones de evacuación reales"*. La
credibilidad de una herramienta de riesgo se construye siendo explícito sobre
lo que aún no sabe hacer.

## 7. Costos de operación (estimados, escala prototipo)

- Infraestructura: $0 (self-hosted, datos públicos, APIs gratuitas con
  límites de cortesía).
- Asesor IA: ~$0.05–0.15 USD por análisis con claude-opus-4-8 (3.7K tokens
  entrada / 2.6K salida medidos) — con caché de 15 min, uso normal < $5/mes.
  Reducible ~5× usando `claude-sonnet-5` vía `AI_ADVISOR_MODEL`.
- Regeneración completa de datos: <1 hora de cómputo en un PC estándar.
