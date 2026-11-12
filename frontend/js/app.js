// Visor 3D del prototipo — terreno real (self-hosted) + imagería satelital Esri.
// No usa Cesium ion: ni terreno, ni imagería, ni geocoder dependen de una cuenta externa.

async function init() {
  // CesiumTerrainProvider.fromUrl queda en un estado inválido si layer.json no
  // existe (rompe el render del globo entero), así que se verifica antes.
  let terrainProvider = new Cesium.EllipsoidTerrainProvider();
  try {
    const check = await fetch("/terrain/layer.json");
    if (check.ok) {
      terrainProvider = await Cesium.CesiumTerrainProvider.fromUrl("/terrain");
    } else {
      console.warn("Terreno 3D aún no generado, usando elipsoide plano.");
    }
  } catch (e) {
    console.warn("No se pudo verificar/cargar el terreno 3D, usando elipsoide plano:", e);
  }

  const viewer = new Cesium.Viewer("cesiumContainer", {
    terrainProvider,
    baseLayer: false,
    baseLayerPicker: false,
    geocoder: false,
    homeButton: true,
    sceneModePicker: false,
    navigationHelpButton: true,
    animation: false,
    timeline: false,
    fullscreenButton: true,
    requestRenderMode: false,
  });

  window.viewer = viewer; // exposición para debugging en consola durante el prototipo

  // El doble clic por defecto de Cesium "trackea" (sigue con la cámara) la
  // entidad picked; lo sacamos porque lo usamos como gesto de "terminar
  // trazo" en la herramienta de dibujar río.
  viewer.screenSpaceEventHandler.removeInputAction(Cesium.ScreenSpaceEventType.LEFT_DOUBLE_CLICK);

  viewer.imageryLayers.addImageryProvider(
    new Cesium.UrlTemplateImageryProvider({
      url: "https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      credit: "Esri, Maxar, Earthstar Geographics",
      maximumLevel: 18,
    })
  );

  try {
    const basinDS = await Cesium.GeoJsonDataSource.load("/api/basin", {
      stroke: Cesium.Color.CYAN,
      fill: Cesium.Color.CYAN.withAlpha(0.04),
      strokeWidth: 3,
      clampToGround: true,
    });
    viewer.dataSources.add(basinDS);
  } catch (e) {
    console.warn("No se pudo cargar el contorno de la cuenca:", e);
  }

  // Solo Ovalle (y cualquier no-embalse) como punto; los embalses se cargan
  // como polígono real más abajo (loadReservoirs).
  try {
    const res = await fetch("/api/markers");
    const data = await res.json();
    for (const m of data.markers || []) {
      if (m.tipo === "embalse") continue;
      viewer.entities.add({
        name: m.nombre,
        position: Cesium.Cartesian3.fromDegrees(m.lon, m.lat),
        point: {
          pixelSize: 10,
          color: Cesium.Color.ORANGE,
          outlineColor: Cesium.Color.WHITE,
          outlineWidth: 2,
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
        },
        label: {
          text: m.nombre + (m.verificado ? "" : " *"),
          font: "13px sans-serif",
          pixelOffset: new Cesium.Cartesian2(0, -18),
          fillColor: Cesium.Color.WHITE,
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 2,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
        },
      });
    }
  } catch (e) {
    console.warn("No se pudieron cargar los marcadores:", e);
  }

  // Vista inicial encuadrando la Región de Coquimbo completa.
  viewer.camera.flyTo({
    destination: Cesium.Rectangle.fromDegrees(-71.9, -32.4, -69.7, -28.95),
    duration: 0,
  });

  const rainfallDataSource = new Cesium.CustomDataSource("rainfall");
  viewer.dataSources.add(rainfallDataSource);

  // === CARGA INICIAL (solo lo esencial para latencia baja) ===
  // Se cargan las capas críticas y ligeras al arranque; las capas pesadas
  // (quebradas, confluencias, badenes, red DEM) se cargan solo cuando el
  // usuario activa su toggle. Con esto la primera vista muestra el mapa en
  // pocos segundos en vez de 20-40s cargando todo por adelantado.
  const [waterwaysDS, situacionDS, placesDS, reservoirsDS] = await Promise.all([
    loadWaterways(viewer),   // 805 tramos ríos permanentes simplificados (~300 KB)
    loadSituacion(viewer),   // 13 puntos rojos de emergencia actual
    loadPlacesRisk(viewer),  // 1141 comunidades (primitives, rápido)
    loadReservoirs(viewer),  // 28 polígonos embalses
  ]);
  setupRiverDrawing(viewer);
  setupRainOverlay();

  // Handles diferidos: se llenan la primera vez que el usuario prende la capa
  const lazy = {
    river: { fn: () => loadRiverNetwork(viewer), ds: null, loading: null },
    quebradas: { fn: () => loadQuebradas(viewer), ds: null, loading: null },
    confluencias: { fn: () => loadConfluencias(viewer), ds: null, loading: null },
    crossings: { fn: () => loadCrossings(viewer), ds: null, loading: null },
    subbasins: { fn: () => loadSubbasins(viewer), ds: null, loading: null },
  };
  async function ensureLazy(key) {
    const slot = lazy[key];
    if (slot.ds) return slot.ds;
    if (!slot.loading) slot.loading = slot.fn();
    slot.ds = await slot.loading;
    return slot.ds;
  }

  // La mancha se recarga con cada cambio de nivel/lluvia (renderFlood crea un
  // DataSource nuevo), así que el toggle guarda el estado en una bandera
  // global que renderFlood respeta al crear cada capa nueva.
  window.__floodVisible = document.getElementById("toggleFlood").checked;
  document.getElementById("toggleFlood").addEventListener("change", (e) => {
    window.__floodVisible = e.target.checked;
    if (window.__floodDataSource) window.__floodDataSource.show = e.target.checked;
  });
  // Handler común: capa cargada eagerly (basta con set .show).
  // También aplica el estado inicial del checkbox al DataSource — así los
  // toggles pueden empezar desmarcados sin dejar la capa visible.
  function bindEager(id, ds) {
    const el = document.getElementById(id);
    if (ds) ds.show = el.checked;
    el.addEventListener("change", (e) => {
      if (ds) ds.show = e.target.checked;
    });
  }
  // Handler común: capa lazy — carga la primera vez que se activa
  function bindLazy(id, key, label) {
    const el = document.getElementById(id);
    el.addEventListener("change", async (e) => {
      if (!e.target.checked) {
        if (lazy[key].ds) lazy[key].ds.show = false;
        return;
      }
      const original = el.parentElement.textContent;
      el.parentElement.title = "Cargando " + label + "...";
      el.disabled = true;
      const ds = await ensureLazy(key);
      el.disabled = false;
      el.parentElement.title = "";
      if (ds) ds.show = true;
      else el.checked = false;
    });
  }

  bindEager("toggleWaterways", waterwaysDS);
  bindEager("togglePlaces", placesDS);
  bindEager("toggleSituacion", situacionDS);
  bindEager("toggleReservoirs", reservoirsDS);
  bindEager("toggleRainfall", rainfallDataSource);
  bindLazy("toggleQuebradas", "quebradas", "quebradas intermitentes");
  bindLazy("toggleConfluencias", "confluencias", "descargas quebrada→río");
  bindLazy("toggleCrossings", "crossings", "badenes y pasos a nivel");
  bindLazy("toggleSubbasins", "subbasins", "sub-cuencas");
  bindLazy("toggleRiver", "river", "red DEM");
  // (subbasins y riverDS quedaron como lazy — se cargan al activar el toggle)

  // --- Nivel manual (slider + presets) ---
  const slider = document.getElementById("waterSlider");
  const sliderOut = document.getElementById("waterSliderOut");
  // BUG-A corregido: el selector antes matcheaba también los presets del modal
  // (#riverModalPresets .presetBtn) y por eso al usar el panel principal se
  // "encendían" los presets del modal y viceversa.
  const presetBtns = Array.from(document.querySelectorAll("#waterPresets .presetBtn"));

  let manualDebounce = null;
  function onSliderInput() {
    const level = parseFloat(slider.value);
    sliderOut.textContent = level.toFixed(1) + " m";
    presetBtns.forEach((b) => b.classList.toggle("active", parseFloat(b.dataset.level) === level));
    clearTimeout(manualDebounce);
    manualDebounce = setTimeout(() => setManualLevel(viewer, level), 150);
  }
  slider.addEventListener("input", onSliderInput);
  presetBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      slider.value = btn.dataset.level;
      onSliderInput();
    });
  });

  // --- Simular lluvia (mm) ---
  const rainSimSlider = document.getElementById("rainSimSlider");
  const rainSimOut = document.getElementById("rainSimOut");
  let rainSimDebounce = null;
  rainSimSlider.addEventListener("input", () => {
    const mm = parseFloat(rainSimSlider.value);
    rainSimOut.textContent = mm.toFixed(0) + " mm";
    clearTimeout(rainSimDebounce);
    if (mm === 0) return;
    rainSimDebounce = setTimeout(() => simulateRainfall(viewer, mm), 200);
  });

  document.getElementById("refreshBtn").addEventListener("click", () => refreshCrecida(viewer, rainfallDataSource, true));
  document.getElementById("aiAnalyzeBtn").addEventListener("click", () => loadAiAnalysis());
  refreshCrecida(viewer, rainfallDataSource, false);
  loadForecast(viewer);

  // --- Seleccionar un tramo de río (o el río dibujado) -> modal con corte de terreno real ---
  {
    const clickHandler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
    clickHandler.setInputAction((click) => {
      if (window.__riverDrawActive) return; // en modo "dibujar río" el clic agrega puntos, no abre el modal
      const picked = viewer.scene.pick(click.position);
      if (!Cesium.defined(picked) || !picked.id) return;
      const riverDS = lazy.river.ds;
      const isRiver = riverDS && riverDS.entities.contains(picked.id);
      const isWaterway = waterwaysDS && waterwaysDS.entities.contains(picked.id);
      const isDrawn = window.__drawnRiverEntity && picked.id === window.__drawnRiverEntity;
      if (!isRiver && !isWaterway && !isDrawn) return;
      const cartesian = pickTerrainPosition(viewer, click.position);
      if (!cartesian) return;
      const carto = Cesium.Cartographic.fromCartesian(cartesian);
      let title;
      if (isDrawn) {
        title = "Corte de terreno — río dibujado";
      } else if (isWaterway) {
        const nombre = picked.id.properties?.nombre?.getValue();
        if (nombre) title = `Corte de terreno — ${nombre}`;
      }
      openRiverModal(
        Cesium.Math.toDegrees(carto.latitude),
        Cesium.Math.toDegrees(carto.longitude),
        title
      );
    }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
  }

  document.getElementById("riverModalClose").addEventListener("click", closeRiverModal);
  document.getElementById("riverModalOverlay").addEventListener("click", (e) => {
    if (e.target.id === "riverModalOverlay") closeRiverModal();
  });

  const riverSlider = document.getElementById("riverWaterSlider");
  riverSlider.addEventListener("input", () => applyRiverWaterLevel(parseFloat(riverSlider.value)));
  document.querySelectorAll("#riverModalPresets .presetBtn").forEach((btn) => {
    btn.addEventListener("click", () => {
      riverSlider.value = btn.dataset.level;
      applyRiverWaterLevel(parseFloat(riverSlider.value));
      document.querySelectorAll("#riverModalPresets .presetBtn").forEach((b) => b.classList.toggle("active", b === btn));
    });
  });

  initWeatherOverlays(viewer);

  // Relaves mineros (bajo demanda — carga /api/relaves al primer toggle)
  const relaves = createRelavesModule(viewer);
  window.__relaves = relaves;
  document.getElementById("toggleRelaves").addEventListener("change", async (e) => {
    if (e.target.checked) await relaves.start();
    else relaves.stop();
  });

  initChat(viewer);
}

// Escala de color simple para mm de lluvia en 24h — no es una escala oficial,
// solo para lectura visual rápida del prototipo.
function rainfallColor(mm) {
  if (mm < 10) return Cesium.Color.fromCssColorString("#7fd3ff").withAlpha(0.7);
  if (mm < 30) return Cesium.Color.fromCssColorString("#3f8efc").withAlpha(0.75);
  if (mm < 60) return Cesium.Color.fromCssColorString("#f4d03f").withAlpha(0.8);
  if (mm < 100) return Cesium.Color.fromCssColorString("#f39c12").withAlpha(0.85);
  return Cesium.Color.fromCssColorString("#e74c3c").withAlpha(0.9);
}

function renderRainfall(rainfallDataSource, data) {
  rainfallDataSource.entities.removeAll();
  for (const p of data.points || []) {
    rainfallDataSource.entities.add({
      position: Cesium.Cartesian3.fromDegrees(p.lon, p.lat),
      ellipse: {
        semiMinorAxis: 4500,
        semiMajorAxis: 4500,
        material: rainfallColor(p.mm_24h),
        height: 0,
        heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
      },
    });
  }
}

async function loadRiverNetwork(viewer) {
  try {
    const ds = await Cesium.GeoJsonDataSource.load("/api/river", {
      stroke: Cesium.Color.fromCssColorString("#1fa8ff"),
      strokeWidth: 3,
      clampToGround: true,
    });
    viewer.dataSources.add(ds);
    return ds;
  } catch (e) {
    console.warn("No se pudo cargar la red de río:", e);
    return null;
  }
}

// Red hídrica permanente de OSM (scripts/08 + scripts/11): solo ríos y
// esteros permanentes, simplificados con Douglas-Peucker ~150m. El backend
// sirve la versión .simple.geojson por defecto (60x más liviana que el crudo)
// — los intermitentes viven en loadQuebradas y los canales de riego no se
// muestran (infraestructura, no cauce natural).
async function loadWaterways(viewer) {
  try {
    const ds = await Cesium.GeoJsonDataSource.load("/api/waterways", {
      clampToGround: true,
    });
    for (const entity of ds.entities.values) {
      if (!entity.polyline) continue;
      const tipo = entity.properties?.tipo?.getValue();
      if (tipo === "river") {
        entity.polyline.material = Cesium.Color.fromCssColorString("#1fa8ff");
        entity.polyline.width = 4;
      } else {
        // stream (permanente)
        entity.polyline.material = Cesium.Color.fromCssColorString("#5fb8e8").withAlpha(0.85);
        entity.polyline.width = 1.6;
      }
    }
    viewer.dataSources.add(ds);
    window.__waterwaysDS = ds;
    return ds;
  } catch (e) {
    console.warn("No se pudo cargar la red hídrica OSM:", e);
    return null;
  }
}

// Quebradas de activación rara (intermittent=yes en OSM): cauces secos casi
// todo el año que solo llevan agua con lluvia intensa. Naranja segmentado
// para distinguirlas de los cauces permanentes; el grosor crece con la
// lluvia activa (= "se están activando").
async function loadQuebradas(viewer) {
  try {
    const ds = await Cesium.GeoJsonDataSource.load("/api/quebradas", {
      clampToGround: true,
    });
    const mat = new Cesium.PolylineDashMaterialProperty({
      color: Cesium.Color.fromCssColorString("#ffab40").withAlpha(0.9),
      dashLength: 12,
    });
    for (const entity of ds.entities.values) {
      if (!entity.polyline) continue;
      entity.polyline.material = mat;
      entity.polyline.width = 1.4;
    }
    viewer.dataSources.add(ds);
    window.__quebradasDS = ds;
    return ds;
  } catch (e) {
    console.warn("No se pudieron cargar las quebradas intermitentes:", e);
    return null;
  }
}

// Capas de puntos como PointPrimitiveCollection / LabelCollection nativas
// en vez de CustomDataSource con Entities: para 1000-3000 puntos, el batch
// primitive es 10-100x más rápido de cargar Y de renderizar cada frame.
// El wrapper expone .show para que los toggles sigan funcionando igual, y
// una API mínima (nombre, count) para el resto del código.
function makePointLayer(viewer, name, primitives, labels) {
  const layer = {
    name,
    _primColl: primitives, // PointPrimitiveCollection
    _labelColl: labels,     // LabelCollection (puede ser null)
    _show: true,
    get show() { return this._show; },
    set show(v) {
      this._show = v;
      this._primColl.show = v;
      if (this._labelColl) this._labelColl.show = v;
    },
    destroy() {
      viewer.scene.primitives.remove(this._primColl);
      if (this._labelColl) viewer.scene.primitives.remove(this._labelColl);
    },
  };
  return layer;
}

// Puntos donde una quebrada intermitente descarga a un cauce permanente:
// ahí es donde una activación repentina inyecta caudal y sedimento al río.
async function loadConfluencias(viewer) {
  try {
    const res = await fetch("/api/confluencias");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const gj = await res.json();
    const points = viewer.scene.primitives.add(new Cesium.PointPrimitiveCollection());
    const color = Cesium.Color.fromCssColorString("#ffe082");
    const outline = Cesium.Color.fromCssColorString("#e65100");
    const scale = new Cesium.NearFarScalar(30000, 1.0, 250000, 0.3);
    for (const f of gj.features || []) {
      const [lon, lat] = f.geometry.coordinates;
      points.add({
        position: Cesium.Cartesian3.fromDegrees(lon, lat),
        pixelSize: 5,
        color,
        outlineColor: outline,
        outlineWidth: 1.5,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
        scaleByDistance: scale,
      });
    }
    return makePointLayer(viewer, "confluencias", points, null);
  } catch (e) {
    console.warn("No se pudieron cargar las confluencias:", e);
    return null;
  }
}

// Comunidades con clasificación de riesgo por cercanía a cauces
// (scripts/09_fetch_places_risk.py — proxy geométrico, no mapa oficial).
const RIESGO_COLORS = {
  alto: Cesium.Color.fromCssColorString("#ff5252"),
  medio: Cesium.Color.fromCssColorString("#ffb74d"),
  bajo: Cesium.Color.fromCssColorString("#9e9e9e"),
};

async function loadPlacesRisk(viewer) {
  try {
    const res = await fetch("/api/places_risk");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const gj = await res.json();
    const points = viewer.scene.primitives.add(new Cesium.PointPrimitiveCollection());
    const labels = viewer.scene.primitives.add(new Cesium.LabelCollection());
    const outline = Cesium.Color.BLACK.withAlpha(0.7);
    const scaleGrande = new Cesium.NearFarScalar(30000, 1.0, 400000, 0.7);
    const scaleChico = new Cesium.NearFarScalar(30000, 1.0, 400000, 0.25);
    for (const f of gj.features || []) {
      const [lon, lat] = f.geometry.coordinates;
      const p = f.properties;
      const esGrande = p.tipo === "city" || p.tipo === "town";
      const pos = Cesium.Cartesian3.fromDegrees(lon, lat);
      points.add({
        position: pos,
        pixelSize: esGrande ? 9 : 6,
        color: RIESGO_COLORS[p.riesgo] || RIESGO_COLORS.bajo,
        outlineColor: outline,
        outlineWidth: 1.5,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
        scaleByDistance: esGrande ? scaleGrande : scaleChico,
      });
      if (esGrande || p.riesgo === "alto") {
        labels.add({
          position: pos,
          text: p.nombre,
          font: (esGrande ? "13px" : "10px") + " sans-serif",
          fillColor: Cesium.Color.WHITE,
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 2,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          pixelOffset: new Cesium.Cartesian2(0, -14),
          distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, esGrande ? 600000 : 120000),
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        });
      }
    }
    return makePointLayer(viewer, "places", points, labels);
  } catch (e) {
    console.warn("No se pudieron cargar las comunidades en riesgo:", e);
    return null;
  }
}

// Situación de emergencia actual recopilada de prensa (data/hydro/
// situacion_actual.json — coordenadas aproximadas a nivel de sector).
async function loadSituacion(viewer) {
  try {
    const res = await fetch("/api/situation");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const ds = new Cesium.CustomDataSource("situacion");
    for (const p of data.puntos_afectados || []) {
      ds.entities.add({
        name: `${p.nombre} — ${p.causa}`,
        position: Cesium.Cartesian3.fromDegrees(p.lon, p.lat),
        point: {
          pixelSize: 12,
          color: Cesium.Color.fromCssColorString("#d50000"),
          outlineColor: Cesium.Color.WHITE,
          outlineWidth: 2.5,
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
        label: {
          text: "⚠ " + p.nombre.split("—")[0].trim(),
          font: "12px sans-serif",
          fillColor: Cesium.Color.fromCssColorString("#ffcdd2"),
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 2,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          pixelOffset: new Cesium.Cartesian2(0, -16),
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
      });
    }
    viewer.dataSources.add(ds);
    window.__situacionData = data; // para el panel
    return ds;
  } catch (e) {
    console.warn("Sin datos de situación actual:", e);
    return null;
  }
}

// Badenes (ford) y pasos a nivel de OSM: los puntos donde un camino cruza el
// agua (o la vía) a nivel — lo primero que se corta en una crecida. 1856
// puntos: se usa PointPrimitiveCollection para no ahogar el renderer.
async function loadCrossings(viewer) {
  try {
    const res = await fetch("/api/crossings");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const gj = await res.json();
    const points = viewer.scene.primitives.add(new Cesium.PointPrimitiveCollection());
    const colorBaden = Cesium.Color.fromCssColorString("#ffb020");
    const colorPaso = Cesium.Color.fromCssColorString("#e2e8f0");
    const outline = Cesium.Color.BLACK.withAlpha(0.8);
    const scale = new Cesium.NearFarScalar(20000, 1.0, 250000, 0.35);
    for (const f of gj.features || []) {
      const [lon, lat] = f.geometry.coordinates;
      const esBaden = f.properties.tipo === "baden";
      points.add({
        position: Cesium.Cartesian3.fromDegrees(lon, lat),
        pixelSize: 7,
        color: esBaden ? colorBaden : colorPaso,
        outlineColor: outline,
        outlineWidth: 1.5,
        disableDepthTestDistance: Number.POSITIVE_INFINITY,
        scaleByDistance: scale,
      });
    }
    return makePointLayer(viewer, "crossings", points, null);
  } catch (e) {
    console.warn("No se pudieron cargar badenes/pasos a nivel:", e);
    return null;
  }
}

async function loadSubbasins(viewer) {
  try {
    const ds = await Cesium.GeoJsonDataSource.load("/api/subbasins", {
      stroke: Cesium.Color.fromCssColorString("#b076f0"),
      fill: Cesium.Color.fromCssColorString("#b076f0").withAlpha(0.08),
      strokeWidth: 1.5,
      clampToGround: true,
    });
    viewer.dataSources.add(ds);
    return ds;
  } catch (e) {
    console.warn("No se pudieron cargar las sub-cuencas:", e);
    return null;
  }
}

async function loadReservoirs(viewer) {
  try {
    const ds = await Cesium.GeoJsonDataSource.load("/api/reservoirs", {
      stroke: Cesium.Color.fromCssColorString("#2b6cb0"),
      fill: Cesium.Color.fromCssColorString("#2b6cb0").withAlpha(0.5),
      strokeWidth: 2,
      clampToGround: true,
    });
    viewer.dataSources.add(ds);
    for (const entity of ds.entities.values) {
      const nombre = entity.properties?.nombre?.getValue();
      if (nombre) {
        entity.label = new Cesium.LabelGraphics({
          text: nombre + " *",
          font: "12px sans-serif",
          fillColor: Cesium.Color.WHITE,
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 2,
          style: Cesium.LabelStyle.FILL_AND_OUTLINE,
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
          pixelOffset: new Cesium.Cartesian2(0, -10),
        });
      }
    }
    return ds;
  } catch (e) {
    console.warn("No se pudieron cargar los embalses:", e);
    return null;
  }
}

// mm de lluvia que corresponden a un floodData, sea cual sea el modo activo
// (auto/simulada: rainfall_mm_24h; pronóstico: cumulative_rainfall_mm; manual: 0,
// no hay lluvia asociada a un nivel fijado a mano).
function currentRainMm(floodData) {
  if (!floodData) return 0;
  if (floodData.rainfall_mm_24h != null) return floodData.rainfall_mm_24h;
  if (floodData.cumulative_rainfall_mm != null) return floodData.cumulative_rainfall_mm;
  return 0;
}

// Grosor (px, para GroundPolylinePrimitive vía clampToGround) del contorno de
// la mancha de inundación y de la red de río: crece con la lluvia para que se
// "note" visualmente que el río se está engrosando, no es una escala real.
function rainGrowthWidth(mm, base, max) {
  return Math.min(base + mm / 12, max);
}

const FLOOD_BLUE = "#2f8dff";

// --- Dibujar río a mano sobre el terreno 3D real ---
// Clic para agregar puntos, doble clic para terminar. El trazo final se
// renderiza como un CorridorGraphics con clampToGround: true, que se amolda
// al relieve real (sube y baja con la malla del terreno) en vez de ser un
// plano genérico. Al terminar se abre el modal Three.js con el corte de
// terreno real (DEM) centrado en el trazo dibujado. El ancho crece con la
// lluvia activa igual que la red de río real (rainGrowthWidth).

// Intersección clic->terreno. globe.pick (rayo contra la malla del terreno)
// es lo confiable acá; pickPosition depende del buffer de profundidad y
// devuelve null sobre terreno "pelado" (sin entidades) en muchos entornos.
// pickEllipsoid queda de último fallback: no considera el relieve, pero como
// todo el trazo se dibuja con clampToGround, el resultado visual se drapea
// igual sobre el terreno.
function pickTerrainPosition(viewer, windowPosition) {
  const ray = viewer.camera.getPickRay(windowPosition);
  if (ray) {
    const hit = viewer.scene.globe.pick(ray, viewer.scene);
    if (hit) return hit;
  }
  if (viewer.scene.pickPositionSupported) {
    const hit = viewer.scene.pickPosition(windowPosition);
    if (hit) return hit;
  }
  return viewer.camera.pickEllipsoid(windowPosition, viewer.scene.globe.ellipsoid) || null;
}

function setupRiverDrawing(viewer) {
  const drawBtn = document.getElementById("drawRiverBtn");
  const clearBtn = document.getElementById("clearRiverBtn");
  const reliefBtn = document.getElementById("viewDrawnReliefBtn");
  if (!drawBtn || !clearBtn) return;

  let points = [];
  let cursorPos = null; // posición bajo el mouse mientras se dibuja
  let previewEntity = null;
  let pointEntities = [];
  let finalEntity = null;
  let handler = null;

  function corridorWidthM(mm) {
    return Math.min(40 + mm * 1.2, 400); // metros reales sobre el terreno
  }

  function startDrawing() {
    clearDrawing();
    window.__riverDrawActive = true;
    if (window.__refreshRainOverlay) window.__refreshRainOverlay(); // llueve mientras se dibuja
    drawBtn.textContent = "✅ Terminar (doble clic)";
    drawBtn.classList.add("active");

    // Línea de vista previa: los puntos ya fijados + el punto flotante bajo
    // el cursor, para que se vea el trazo desde el primer clic.
    previewEntity = viewer.entities.add({
      polyline: {
        positions: new Cesium.CallbackProperty(() => {
          const list = cursorPos ? [...points, cursorPos] : points;
          return list.length >= 2 ? list : undefined;
        }, false),
        width: 4,
        clampToGround: true,
        material: Cesium.Color.fromCssColorString("#1fa8ff"),
      },
    });

    handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);

    handler.setInputAction((click) => {
      const cartesian = pickTerrainPosition(viewer, click.position);
      if (!cartesian) return; // clic fuera del terreno (cielo/horizonte)
      // El doble clic dispara también dos LEFT_CLICK: no duplicar el último punto.
      if (points.length > 0 && Cesium.Cartesian3.distance(points[points.length - 1], cartesian) < 5) return;
      points.push(cartesian);
      pointEntities.push(
        viewer.entities.add({
          position: cartesian,
          point: {
            pixelSize: 8,
            color: Cesium.Color.fromCssColorString("#1fa8ff"),
            outlineColor: Cesium.Color.WHITE,
            outlineWidth: 2,
            heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
            disableDepthTestDistance: Number.POSITIVE_INFINITY,
          },
        })
      );
    }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

    handler.setInputAction((movement) => {
      cursorPos = pickTerrainPosition(viewer, movement.endPosition);
    }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);

    handler.setInputAction(() => finishDrawing(), Cesium.ScreenSpaceEventType.LEFT_DOUBLE_CLICK);
  }

  function finishDrawing() {
    if (handler) {
      handler.destroy();
      handler = null;
    }
    window.__riverDrawActive = false;
    if (window.__refreshRainOverlay) window.__refreshRainOverlay();
    cursorPos = null;
    drawBtn.textContent = "✏️ Dibujar río";
    drawBtn.classList.remove("active");

    if (previewEntity) {
      viewer.entities.remove(previewEntity);
      previewEntity = null;
    }
    pointEntities.forEach((e) => viewer.entities.remove(e));
    pointEntities = [];
    if (points.length < 2) {
      points = [];
      return;
    }

    const mm = currentRainMm(window.__lastFloodData);
    finalEntity = viewer.entities.add({
      corridor: {
        positions: points.slice(),
        width: corridorWidthM(mm),
        cornerType: Cesium.CornerType.ROUNDED,
        clampToGround: true,
        material: Cesium.Color.fromCssColorString(FLOOD_BLUE).withAlpha(0.65),
        outline: true,
        outlineColor: Cesium.Color.fromCssColorString("#0d6fc4"),
      },
    });
    window.__drawnRiverEntity = finalEntity;
    window.__updateDrawnRiverWidth = (newMm) => {
      if (finalEntity && finalEntity.corridor) {
        finalEntity.corridor.width = corridorWidthM(newMm);
      }
    };

    // Igual que al hacer clic en la red de río: se abre la ventana con el
    // relieve real del DEM, centrada en el punto medio del trazo dibujado.
    // El punto medio queda guardado para poder reabrirla con el botón
    // "Ver relieve 3D" sin tener que redibujar.
    const mid = points[Math.floor(points.length / 2)];
    const carto = Cesium.Cartographic.fromCartesian(mid);
    window.__drawnRiverMid = {
      lat: Cesium.Math.toDegrees(carto.latitude),
      lon: Cesium.Math.toDegrees(carto.longitude),
    };
    if (reliefBtn) reliefBtn.disabled = false;
    openDrawnRelief();
  }

  function openDrawnRelief() {
    const mid = window.__drawnRiverMid;
    if (!mid) return;
    openRiverModal(mid.lat, mid.lon, "Corte de terreno — río dibujado");
  }

  function clearDrawing() {
    if (handler) {
      handler.destroy();
      handler = null;
    }
    if (previewEntity) {
      viewer.entities.remove(previewEntity);
      previewEntity = null;
    }
    pointEntities.forEach((e) => viewer.entities.remove(e));
    pointEntities = [];
    if (finalEntity) {
      viewer.entities.remove(finalEntity);
      finalEntity = null;
    }
    points = [];
    cursorPos = null;
    window.__riverDrawActive = false;
    if (window.__refreshRainOverlay) window.__refreshRainOverlay();
    window.__drawnRiverEntity = null;
    window.__updateDrawnRiverWidth = null;
    window.__drawnRiverMid = null;
    if (reliefBtn) reliefBtn.disabled = true;
    drawBtn.textContent = "✏️ Dibujar río";
    drawBtn.classList.remove("active");
  }

  drawBtn.addEventListener("click", () => {
    if (window.__riverDrawActive) finishDrawing();
    else startDrawing();
  });
  clearBtn.addEventListener("click", clearDrawing);
  if (reliefBtn) reliefBtn.addEventListener("click", openDrawnRelief);
}

async function renderFlood(viewer, currentFloodDS, floodData) {
  const old = currentFloodDS;
  let ds = null;
  const hasFeatures = floodData.geojson && floodData.geojson.features && floodData.geojson.features.length > 0;
  if (hasFeatures) {
    const mm = currentRainMm(floodData);
    ds = await Cesium.GeoJsonDataSource.load(floodData.geojson, {
      stroke: Cesium.Color.fromCssColorString(FLOOD_BLUE),
      fill: Cesium.Color.fromCssColorString(FLOOD_BLUE).withAlpha(0.35),
      strokeWidth: rainGrowthWidth(mm, 2, 14),
      clampToGround: true,
    });
    ds.name = "flood"; // para poder identificarlo
    ds.show = window.__floodVisible !== false;
    viewer.dataSources.add(ds);
  }
  // Se agrega la nueva capa antes de sacar la vieja para evitar el parpadeo.
  // BUG-F: si la respuesta no tiene features (delta_h≈0 → área 0), NO borrar
  // la mancha vieja: mantener la última visible mejor que dejar el mapa vacío
  // por un instante.
  if (old && hasFeatures) {
    viewer.dataSources.remove(old, true);
  }
  updateRainDependentWidths(currentRainMm(floodData));
  return ds || old;
}

// Engruesa la red de río real y el trazo dibujado a mano según la lluvia
// activa (mismo mm que mueve el contorno de la mancha de inundación).
function updateRainDependentWidths(mm) {
  const w = rainGrowthWidth(mm, 3, 16);
  if (window.__riverDS) {
    for (const entity of window.__riverDS.entities.values) {
      if (entity.polyline) entity.polyline.width = w;
    }
  }
  if (window.__waterwaysDS) {
    const wRiver = rainGrowthWidth(mm, 4, 18);
    const wStream = rainGrowthWidth(mm, 1.6, 8);
    for (const entity of window.__waterwaysDS.entities.values) {
      if (!entity.polyline) continue;
      const tipo = entity.properties?.tipo?.getValue();
      if (tipo === "river") entity.polyline.width = wRiver;
      else if (tipo === "stream") entity.polyline.width = wStream;
      // canales/drenajes no crecen: son infraestructura, no cauce natural
    }
  }
  if (window.__quebradasDS) {
    // las quebradas intermitentes "se activan" con la lluvia: casi invisibles
    // en seco, gruesas y llamativas cuando el evento es fuerte
    const wQueb = rainGrowthWidth(mm, 1.4, 7);
    for (const entity of window.__quebradasDS.entities.values) {
      if (entity.polyline) entity.polyline.width = wQueb;
    }
  }
  if (window.__updateDrawnRiverWidth) {
    window.__updateDrawnRiverWidth(mm);
  }
  if (window.__setRainIntensity) {
    window.__setRainIntensity(mm);
  }
}

// --- Lluvia animada sobre el mapa principal ---
// Canvas 2D superpuesto al visor (pointer-events: none): gotas que caen con
// intensidad proporcional a los mm activos. Se enciende sola al simular
// lluvia / pronóstico / lluvia real > 0, y también mientras se dibuja un río
// (para "ver llover" mientras se traza el cauce, aunque haya 0mm activos).
function setupRainOverlay() {
  const container = document.getElementById("cesiumContainer");
  const canvas = document.createElement("canvas");
  canvas.id = "rainOverlay";
  container.appendChild(canvas);
  const ctx = canvas.getContext("2d");

  let drops = [];
  let rafId = null;
  let lastMm = 0;

  function resize() {
    canvas.width = container.clientWidth;
    canvas.height = container.clientHeight;
  }
  window.addEventListener("resize", resize);
  resize();

  function makeDrop() {
    return {
      x: Math.random() * canvas.width,
      y: Math.random() * canvas.height,
      len: 8 + Math.random() * 10,
      vx: 0.4 + Math.random() * 0.5,
      vy: 7 + Math.random() * 6,
    };
  }

  function frame() {
    rafId = requestAnimationFrame(frame);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = "rgba(165, 210, 255, 0.55)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (const d of drops) {
      ctx.moveTo(d.x, d.y);
      ctx.lineTo(d.x + d.vx * 1.5, d.y + d.len);
      d.x += d.vx;
      d.y += d.vy;
      if (d.y > canvas.height) {
        d.y = -20 - Math.random() * 40;
        d.x = Math.random() * canvas.width;
      }
    }
    ctx.stroke();
  }

  function apply() {
    const forced = window.__riverDrawActive ? 150 : 0;
    const byRain = lastMm <= 0 ? 0 : Math.min(60 + lastMm * 3, 800);
    const target = Math.max(forced, Math.round(byRain));
    while (drops.length < target) drops.push(makeDrop());
    drops.length = target;
    if (target > 0 && rafId === null) {
      canvas.style.display = "block";
      frame();
    } else if (target === 0 && rafId !== null) {
      cancelAnimationFrame(rafId);
      rafId = null;
      canvas.style.display = "none";
    }
  }

  window.__setRainIntensity = (mm) => {
    lastMm = mm;
    apply();
  };
  // el modo dibujo fuerza lluvia visible; se re-evalúa al entrar/salir
  window.__refreshRainOverlay = apply;
}

function floodStatusLabel(pct) {
  if (pct < 1) return { text: "Normal", color: "#8fe3a0" };
  if (pct < 3) return { text: "Alerta", color: "#f4d03f" };
  return { text: "Crecida mayor", color: "#ff5b5b" };
}

const MODE_LABELS = {
  manual: "manual (slider)",
  simulated_rainfall: "simulación de lluvia",
  forecast_cumulative: "pronóstico acumulado",
};

function updatePanel(rainData, floodData, error) {
  const body = document.getElementById("floodPanelBody");
  if (error) {
    body.innerHTML = `<span class="warn">No se pudo actualizar: ${escapeHtml(error)}</span>`;
    return;
  }
  window.__lastFloodData = floodData; // para inicializar el slider del modal de río
  // Reevaluar relaves cuando cambia el flood state (slider, lluvia, pronóstico)
  if (window.__relaves) window.__relaves.onFloodStateChanged();
  const status = floodStatusLabel(floodData.flooded_pct_of_basin);
  let modeLine;
  if (floodData.mode === "simulated_rainfall") {
    modeLine = `Lluvia simulada: <b>${escapeHtml(floodData.rainfall_mm_24h)} mm</b>`;
  } else if (floodData.mode === "forecast_cumulative") {
    modeLine = `Pronóstico acumulado (día ${escapeHtml(floodData.forecast_day)}): <b>${escapeHtml(floodData.cumulative_rainfall_mm)} mm</b>`;
  } else if (floodData.mode === "manual") {
    modeLine = `Modo: <b>manual (slider)</b>`;
  } else {
    modeLine = `Lluvia promedio regional (24h): <b>${rainData ? escapeHtml(rainData.basin_average_mm) : "?"} mm</b>`;
  }
  const fetchedAt = rainData ? new Date(rainData.fetched_at).toLocaleString("es-CL") : "";

  body.innerHTML = `
    ${modeLine}<br/>
    Alza de nivel: <b>${escapeHtml(floodData.delta_h_m)} m</b>${floodData.delta_h_capped ? " (tope)" : ""}<br/>
    Área inundable estimada: <b>${escapeHtml(floodData.flooded_area_km2)} km²</b>
    (${escapeHtml(floodData.flooded_pct_of_basin)}% de la cuenca)<br/>
    Estado: <b style="color:${status.color}">${status.text}</b><br/>
    ${fetchedAt ? `<span style="opacity:0.7; font-size:11px;">Lluvia actualizada ${fetchedAt}</span>` : ""}
    <span class="warn">Estimación gruesa, no calibrada. NO usar para decisiones de evacuación.</span>
  `;
}

function clearOtherControls(except) {
  if (except !== "manual") {
    // BUG-A corregido: solo los presets del panel principal, no los del modal
    document.querySelectorAll("#waterPresets .presetBtn").forEach((b) => b.classList.remove("active"));
  }
  if (except !== "rainsim") {
    document.getElementById("rainSimSlider").value = 0;
    document.getElementById("rainSimOut").textContent = "0 mm";
  }
  if (except !== "forecast") {
    document.querySelectorAll(".dayBtn").forEach((b) => b.classList.remove("active"));
  }
}

// Cada slider/botón dispara muchos eventos seguidos (arrastrar el mouse);
// sin esto, una respuesta vieja que llega después de una más nueva podía
// "pisar" el resultado correcto y el mapa se quedaba mostrando un nivel
// anterior — se veía como si subir los mm no hiciera nada.
window.__floodReqId = 0;

async function updateFlood(viewer, fetchUrl, exceptControl) {
  const myId = ++window.__floodReqId;
  try {
    const res = await fetch(fetchUrl);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const floodData = await res.json();
    // BUG-D corregido: chequear obsolescencia ANTES de tocar __floodDataSource.
    // Si no, dos requests en carrera dejaban la referencia global desincronizada
    // con lo que realmente está en el viewer (aunque el chequeo tardío devuelva
    // null, la asignación ya había cambiado la referencia).
    if (myId !== window.__floodReqId) return null;
    const newDS = await renderFlood(viewer, window.__floodDataSource, floodData);
    if (myId !== window.__floodReqId) {
      // Mientras renderizaba llegó otra request más nueva. La nuestra ya se
      // agregó al viewer — la removemos para no dejar manchas huérfanas.
      if (newDS && newDS !== window.__floodDataSource) {
        viewer.dataSources.remove(newDS, true);
      }
      return null;
    }
    window.__floodDataSource = newDS;
    updatePanel(window.__lastRainData, floodData, null);
    clearOtherControls(exceptControl);
    return floodData;
  } catch (e) {
    console.error("Error actualizando inundación:", e);
    return null;
  }
}

function setManualLevel(viewer, level) {
  return updateFlood(viewer, `/api/flood?delta_h=${level}`, "manual");
}

function simulateRainfall(viewer, mm) {
  return updateFlood(viewer, `/api/flood?rainfall_mm=${mm}`, "rainsim");
}

async function loadForecast(viewer) {
  const container = document.getElementById("forecastDays");
  try {
    const res = await fetch("/api/forecast");
    const data = await res.json();
    container.innerHTML = "";
    data.days.forEach((d, idx) => {
      const dayNum = idx + 1;
      const date = new Date(d.date + "T00:00:00");
      const label = date.toLocaleDateString("es-CL", { weekday: "short" });
      const btn = document.createElement("button");
      btn.className = "dayBtn";
      btn.innerHTML = `${label}<span class="mm">${d.mm.toFixed(0)}mm</span>`;
      btn.addEventListener("click", async () => {
        const floodData = await updateFlood(viewer, `/api/flood/forecast_day?day=${dayNum}`, "forecast");
        if (!floodData) return;
        document.querySelectorAll(".dayBtn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
      });
      container.appendChild(btn);
    });
  } catch (e) {
    container.textContent = "No se pudo cargar el pronóstico.";
    console.warn("Error cargando pronóstico:", e);
  }
}

// --- Asesor IA (Claude) ---
// Pide al backend el análisis de riesgo generado por Claude a partir de la
// lluvia actual, el pronóstico, el modelo HAND, los registros históricos y
// los puntos críticos (badenes/embalses). Puede tardar 30-90s: el modelo
// razona sobre todo el contexto. El backend cachea 15 min.
function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

async function loadAiAnalysis() {
  const btn = document.getElementById("aiAnalyzeBtn");
  const body = document.getElementById("aiAnalysisBody");
  btn.disabled = true;
  btn.textContent = "🤖 Analizando… (puede tardar ~1 min)";
  try {
    const res = await fetch("/api/ai_analysis");
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);

    const a = data.analysis;
    const zonas = (a.zonas_en_riesgo || [])
      .map((z) => `<li><b>${escapeHtml(z.lugar)}</b>: ${escapeHtml(z.motivo)}</li>`)
      .join("");
    const recs = (a.recomendaciones || []).map((r) => `<li>${escapeHtml(r)}</li>`).join("");
    const genLocal = new Date(data.generated_at).toLocaleString("es-CL");

    body.innerHTML = `
      <span class="aiAlertChip ${escapeHtml(a.nivel_alerta)}">Alerta ${escapeHtml(a.nivel_alerta)}</span>
      <div><b>${escapeHtml(a.titulo)}</b></div>
      <div>${escapeHtml(a.resumen)}</div>
      <h4>Análisis del clima</h4>
      <div>${escapeHtml(a.analisis_climatico)}</div>
      <h4>Comparación histórica</h4>
      <div>${escapeHtml(a.comparacion_historica)}</div>
      <h4>Zonas en riesgo</h4>
      <ul>${zonas || "<li>Sin zonas críticas identificadas.</li>"}</ul>
      <h4>Recomendaciones</h4>
      <ul>${recs}</ul>
      <h4>Evacuación preventiva</h4>
      <div>${escapeHtml(a.evacuacion_preventiva)}</div>
      <div class="aiMeta">${escapeHtml(a.disclaimer)}<br/>
        Generado ${escapeHtml(genLocal)} · ${escapeHtml(data.model)} ·
        lluvia 24h ${escapeHtml(data.context_summary.lluvia_24h_mm)}mm ·
        pronóstico 7d ${escapeHtml(data.context_summary.pronostico_acumulado_7d_mm)}mm
      </div>
    `;
  } catch (e) {
    body.innerHTML = `<div class="aiError">${escapeHtml(e.message || String(e))}</div>`;
    console.error("Error en análisis IA:", e);
  } finally {
    btn.disabled = false;
    btn.textContent = "🤖 Generar análisis de riesgo";
  }
}

async function refreshCrecida(viewer, rainfallDataSource, force) {
  const btn = document.getElementById("refreshBtn");
  btn.disabled = true;
  btn.textContent = "Actualizando…";
  const myId = ++window.__floodReqId;
  try {
    const [rainRes, floodRes] = await Promise.all([
      fetch(`/api/rainfall${force ? "?refresh=true" : ""}`),
      fetch(`/api/flood${force ? "?refresh=true" : ""}`),
    ]);
    const rainData = await rainRes.json();
    const floodData = await floodRes.json();
    if (myId !== window.__floodReqId) return; // una interacción manual más nueva ganó la carrera
    window.__lastRainData = rainData;
    renderRainfall(rainfallDataSource, rainData);
    window.__floodDataSource = await renderFlood(viewer, window.__floodDataSource, floodData);
    updatePanel(rainData, floodData, null);

    // Sincroniza el slider manual con el nivel automático recién calculado.
    const slider = document.getElementById("waterSlider");
    const sliderOut = document.getElementById("waterSliderOut");
    slider.value = floodData.delta_h_m;
    sliderOut.textContent = floodData.delta_h_m.toFixed(1) + " m";
    clearOtherControls(null);
  } catch (e) {
    console.error("Error actualizando lluvia/inundación:", e);
    updatePanel(null, null, e.message || String(e));
  } finally {
    btn.disabled = false;
    btn.textContent = "Actualizar según lluvia actual";
  }
}

// --- Modal "corte de terreno" — Three.js con datos reales del DEM ---

async function openRiverModal(lat, lon, customTitle) {
  const overlay = document.getElementById("riverModalOverlay");
  const title = document.getElementById("riverModalTitle");
  overlay.classList.add("open");
  title.textContent = "Corte de terreno — cargando…";

  try {
    const res = await fetch(`/api/terrain_profile?lat=${lat}&lon=${lon}&size_km=3`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const profile = await res.json();
    title.textContent = customTitle || "Corte de terreno";
    initRiverScene(profile);

    const startLevel = window.__lastFloodData
      ? Math.min(window.__lastFloodData.delta_h_m, 20)
      : 1;
    const slider = document.getElementById("riverWaterSlider");
    slider.value = startLevel;
    applyRiverWaterLevel(startLevel);
  } catch (e) {
    title.textContent = "No se pudo cargar el terreno local";
    console.error("Error cargando terrain_profile:", e);
  }
}

function closeRiverModal() {
  document.getElementById("riverModalOverlay").classList.remove("open");
  if (window.__riverScene) {
    window.__riverScene.dispose();
    window.__riverScene = null;
  }
}

// Arma una textura con la imagería satelital real de Esri (la misma del mapa
// principal) cubriendo exactamente el parche del corte de terreno, cosiendo
// las teselas web-mercator necesarias en un canvas. Sobre un parche de ~3km
// la diferencia mercator vs grilla lineal de latitud es despreciable.
// Si alguna tesela falla (sin red, CORS), no se llama onReady y el terreno
// se queda con los colores por elevación de siempre.
function loadSatelliteTexture(profile, onReady) {
  const lat = profile.center.lat;
  const sizeM = profile.size_km * 1000;
  let bounds = profile.bounds;
  if (!bounds) {
    // backend viejo sin bounds: se recalculan con la misma fórmula
    const halfLat = sizeM / 2 / 111320;
    const halfLon = sizeM / 2 / (111320 * Math.cos((lat * Math.PI) / 180));
    bounds = {
      west: profile.center.lon - halfLon,
      east: profile.center.lon + halfLon,
      south: lat - halfLat,
      north: lat + halfLat,
    };
  }

  // zoom tal que el parche quede en ~700px por lado (nitidez sin bajar 20 teselas)
  const mpp0 = 156543.03392 * Math.cos((lat * Math.PI) / 180);
  const z = Math.min(Math.max(Math.round(Math.log2((mpp0 * 700) / sizeM)), 12), 17);

  const worldPx = Math.pow(2, z) * 256;
  const lonToX = (lon) => ((lon + 180) / 360) * worldPx;
  const latToY = (la) => {
    const r = (la * Math.PI) / 180;
    return ((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2) * worldPx;
  };
  const x0 = lonToX(bounds.west);
  const x1 = lonToX(bounds.east);
  const y0 = latToY(bounds.north);
  const y1 = latToY(bounds.south);

  const canvas = document.createElement("canvas");
  canvas.width = Math.max(Math.round(x1 - x0), 1);
  canvas.height = Math.max(Math.round(y1 - y0), 1);
  const ctx = canvas.getContext("2d");

  const tx0 = Math.floor(x0 / 256);
  const tx1 = Math.floor((x1 - 1) / 256);
  const ty0 = Math.floor(y0 / 256);
  const ty1 = Math.floor((y1 - 1) / 256);
  let pending = 0;
  let failed = false;
  for (let tx = tx0; tx <= tx1; tx++) {
    for (let ty = ty0; ty <= ty1; ty++) {
      pending++;
      const img = new Image();
      img.crossOrigin = "anonymous";
      img.onload = () => {
        ctx.drawImage(img, Math.round(tx * 256 - x0), Math.round(ty * 256 - y0));
        pending--;
        if (pending === 0 && !failed) onReady(canvas);
      };
      img.onerror = () => {
        failed = true;
        pending--;
        console.warn("No se pudo cargar imagería satelital para el corte de terreno.");
      };
      img.src = `https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/tile/${z}/${ty}/${tx}`;
    }
  }
}

function initRiverScene(profile) {
  if (window.__riverScene) {
    window.__riverScene.dispose();
    window.__riverScene = null;
  }

  const wrap = document.getElementById("riverCanvasWrap");
  wrap.innerHTML = "";
  const W = wrap.clientWidth || 600;
  const H = wrap.clientHeight || 320;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, W / H, 0.1, 5000);
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setSize(W, H);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  wrap.appendChild(renderer.domElement);

  scene.add(new THREE.AmbientLight(0xffffff, 0.7));
  const sun = new THREE.DirectionalLight(0xffffff, 0.8);
  sun.position.set(150, 200, 100);
  scene.add(sun);

  const gridN = profile.grid_n;
  const seg = gridN - 1;
  const SIZE = 400; // unidades visuales de la escena, no metros reales
  const geo = new THREE.PlaneGeometry(SIZE, SIZE, seg, seg);
  geo.rotateX(-Math.PI / 2);
  const pos = geo.attributes.position;

  const heights = profile.heights;
  const minElev = profile.min_elev_m;
  const maxElev = profile.max_elev_m;
  const elevRange = Math.max(maxElev - minElev, 1);
  const VSCALE = 70 / elevRange; // exageración vertical para que el relieve se note

  function sampleHeight(x, z) {
    let col = Math.round((x / SIZE + 0.5) * seg);
    let row = Math.round((z / SIZE + 0.5) * seg);
    col = Math.min(Math.max(col, 0), seg);
    row = Math.min(Math.max(row, 0), seg);
    return heights[row * gridN + col];
  }

  const realHeights = new Float32Array(pos.count);
  const colors = [];
  const col_ = new THREE.Color();
  for (let i = 0; i < pos.count; i++) {
    const realH = sampleHeight(pos.getX(i), pos.getZ(i));
    realHeights[i] = realH;
    pos.setY(i, (realH - minElev) * VSCALE);

    const t = (realH - minElev) / elevRange;
    if (t < 0.08) col_.set(0x5dcaa5);
    else if (t < 0.35) col_.set(0x97c459);
    else if (t < 0.65) col_.set(0xba7517);
    else col_.set(0x888780);
    colors.push(col_.r, col_.g, col_.b);
  }
  geo.computeVertexNormals();
  geo.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));

  const terrainMat = new THREE.MeshLambertMaterial({ vertexColors: true });
  const terrain = new THREE.Mesh(geo, terrainMat);
  scene.add(terrain);

  // Drapear la imagería satelital real sobre el relieve (asíncrono; mientras
  // llega — o si falla — quedan los colores por elevación). El canvas tiene
  // el norte en la fila 0 y con flipY (default) cae en v=1, que tras el
  // rotateX(-PI/2) del plano corresponde a -Z: el mismo lado que la fila 0
  // de heights en sampleHeight. Orientación consistente sin ajustes.
  loadSatelliteTexture(profile, (canvas) => {
    if (window.__riverScene && window.__riverScene.water !== water) return; // el modal ya se recargó con otro parche
    const tex = new THREE.CanvasTexture(canvas);
    terrainMat.map = tex;
    terrainMat.vertexColors = false;
    terrainMat.needsUpdate = true;
  });

  // --- Superficie de agua que sigue el cauce real ---
  // Base local del cauce por celda ("HAND local" aproximado): la elevación
  // mínima en una ventana de ~300m alrededor de cada celda — el fondo del
  // valle o quebrada más cercano según el propio DEM. El agua queda a
  // `nivel` metros sobre esa base, así la inundación se pega al trazado
  // real del río (con sus curvas y quebradas tributarias) y avanza cauce
  // abajo, en vez de llenar el parche como una laguna horizontal o en
  // franjas rectas. Como base <= terreno en cada celda (es un mínimo que
  // incluye la celda misma), con nivel bajo solo se moja la franja pegada
  // al cauce.
  const R = Math.max(6, Math.round(gridN / 10)); // ~300m con gridN=128 sobre 3km
  const minRows = new Float64Array(gridN * gridN);
  for (let r = 0; r < gridN; r++) {
    for (let c = 0; c < gridN; c++) {
      let m = Infinity;
      for (let cc = Math.max(0, c - R); cc <= Math.min(gridN - 1, c + R); cc++) {
        const h = heights[r * gridN + cc];
        if (h < m) m = h;
      }
      minRows[r * gridN + c] = m;
    }
  }
  const baseGrid = new Float64Array(gridN * gridN);
  for (let c = 0; c < gridN; c++) {
    for (let r = 0; r < gridN; r++) {
      let m = Infinity;
      for (let rr = Math.max(0, r - R); rr <= Math.min(gridN - 1, r + R); rr++) {
        const h = minRows[rr * gridN + c];
        if (h < m) m = h;
      }
      baseGrid[r * gridN + c] = m;
    }
  }
  // suavizado leve 3x3 para que la superficie del agua no quede escalonada
  const baseSmooth = new Float64Array(gridN * gridN);
  for (let r = 0; r < gridN; r++) {
    for (let c = 0; c < gridN; c++) {
      let s = 0;
      let n = 0;
      for (let rr = Math.max(0, r - 1); rr <= Math.min(gridN - 1, r + 1); rr++) {
        for (let cc = Math.max(0, c - 1); cc <= Math.min(gridN - 1, c + 1); cc++) {
          s += baseGrid[rr * gridN + cc];
          n++;
        }
      }
      baseSmooth[r * gridN + c] = s / n;
    }
  }

  const waterGeo = new THREE.PlaneGeometry(SIZE, SIZE, seg, seg);
  waterGeo.rotateX(-Math.PI / 2);
  const waterPos = waterGeo.attributes.position;
  // base del cauce por vértice del agua (mismo mapeo x/z -> col/fila que el terreno)
  const waterBase = new Float64Array(waterPos.count);
  for (let i = 0; i < waterPos.count; i++) {
    let col = Math.round((waterPos.getX(i) / SIZE + 0.5) * seg);
    let row = Math.round((waterPos.getZ(i) / SIZE + 0.5) * seg);
    col = Math.min(Math.max(col, 0), seg);
    row = Math.min(Math.max(row, 0), seg);
    waterBase[i] = baseSmooth[row * gridN + col];
  }
  const waterMat = new THREE.MeshPhongMaterial({
    color: 0x378add,
    transparent: true,
    opacity: 0.75,
    shininess: 60,
  });
  const water = new THREE.Mesh(waterGeo, waterMat);
  scene.add(water);

  // --- Gotas de lluvia cayendo sobre el territorio ---
  // Siempre hay una lluvia base al abrir (es un simulador de crecidas por
  // lluvia); la densidad crece con los mm activos del panel principal.
  const mmRain = currentRainMm(window.__lastFloodData);
  const dropCount = Math.round(Math.min(300 + mmRain * 8, 2500));
  const RAIN_TOP = 140;
  const rainPositions = new Float32Array(dropCount * 3);
  const rainSpeeds = new Float32Array(dropCount);
  for (let i = 0; i < dropCount; i++) {
    rainPositions[i * 3] = (Math.random() - 0.5) * SIZE;
    rainPositions[i * 3 + 1] = Math.random() * RAIN_TOP;
    rainPositions[i * 3 + 2] = (Math.random() - 0.5) * SIZE;
    rainSpeeds[i] = 1.3 + Math.random() * 1.1;
  }
  const rainGeo = new THREE.BufferGeometry();
  rainGeo.setAttribute("position", new THREE.BufferAttribute(rainPositions, 3));
  const rainMat = new THREE.PointsMaterial({
    color: 0xa8cfff,
    size: 1.6,
    transparent: true,
    opacity: 0.65,
    sizeAttenuation: false,
  });
  const rain = new THREE.Points(rainGeo, rainMat);
  scene.add(rain);

  let radius = 260;
  let theta = Math.PI * 0.28;
  let phi = Math.PI * 0.35;
  function updateCamera() {
    camera.position.set(
      radius * Math.sin(phi) * Math.cos(theta),
      radius * Math.cos(phi),
      radius * Math.sin(phi) * Math.sin(theta)
    );
    camera.lookAt(0, 20, 0);
  }
  updateCamera();

  let dragging = false;
  let lastX = 0;
  let lastY = 0;
  const onDown = (e) => {
    dragging = true;
    lastX = e.clientX;
    lastY = e.clientY;
  };
  const onUp = () => {
    dragging = false;
  };
  const onMove = (e) => {
    if (!dragging) return;
    const dx = e.clientX - lastX;
    const dy = e.clientY - lastY;
    theta -= dx * 0.006;
    phi = Math.min(Math.max(phi - dy * 0.004, 0.15), 1.4);
    lastX = e.clientX;
    lastY = e.clientY;
    updateCamera();
  };
  const onWheel = (e) => {
    e.preventDefault(); // que la rueda haga zoom en la escena, no scroll del modal
    radius = Math.min(Math.max(radius * (1 + e.deltaY * 0.0012), 60), 700);
    updateCamera();
  };
  wrap.addEventListener("pointerdown", onDown);
  window.addEventListener("pointerup", onUp);
  window.addEventListener("pointermove", onMove);
  wrap.addEventListener("wheel", onWheel, { passive: false });

  let rafId = null;
  function renderLoop() {
    rafId = requestAnimationFrame(renderLoop);
    // animar la caída de las gotas
    const rp = rainGeo.attributes.position;
    for (let i = 0; i < dropCount; i++) {
      let y = rp.getY(i) - rainSpeeds[i];
      if (y < 0) y = RAIN_TOP;
      rp.setY(i, y);
    }
    rp.needsUpdate = true;
    renderer.render(scene, camera);
  }
  renderLoop();

  window.__riverScene = {
    water,
    waterPos,
    waterGeo,
    waterBase,
    realHeights,
    minElev,
    maxElev,
    VSCALE,
    dispose() {
      cancelAnimationFrame(rafId);
      wrap.removeEventListener("pointerdown", onDown);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointermove", onMove);
      wrap.removeEventListener("wheel", onWheel);
      renderer.dispose();
    },
  };
}

function applyRiverWaterLevel(level) {
  const rs = window.__riverScene;
  if (!rs) return;
  // El agua queda `level` metros sobre el perfil del cauce en cada sección
  // del valle (no un plano horizontal — ver comentario en initRiverScene).
  for (let i = 0; i < rs.waterPos.count; i++) {
    rs.waterPos.setY(i, (rs.waterBase[i] + level - rs.minElev) * rs.VSCALE);
  }
  rs.waterPos.needsUpdate = true;
  document.getElementById("riverWaterOut").textContent = level.toFixed(1) + " m";

  let flooded = 0;
  for (let i = 0; i < rs.realHeights.length; i++) {
    if (rs.realHeights[i] < rs.waterBase[i] + level) flooded++;
  }
  const pct = (100 * flooded) / rs.realHeights.length;
  // con la base pegada al cauce, niveles bajos mojan una cinta angosta
  // (<1% del parche): mostrar un decimal para que no se lea "0%"
  document.getElementById("riverFloodPct").textContent =
    (pct < 10 ? pct.toFixed(1) : Math.round(pct)) + "%";

  const statusEl = document.getElementById("riverFloodStatus");
  let status, color;
  if (pct < 8) {
    status = "Normal";
    color = "#8fe3a0";
  } else if (pct < 20) {
    status = "Alerta";
    color = "#f4d03f";
  } else {
    status = "Crecida mayor";
    color = "#ff5b5b";
  }
  statusEl.textContent = status;
  statusEl.style.color = color;
}

init();

// --- Chatbot conversacional ---
// Panel flotante que habla con /api/chat (Claude + tool use). El backend
// devuelve texto y una lista de `actions` que ejecutamos acá sobre el visor:
// volar la cámara, mover el slider de nivel/lluvia, abrir el corte 3D, etc.
// Historial se mantiene solo en memoria del cliente y se envía en cada turno.

function initChat(viewer) {
  const toggle = document.getElementById("chatToggle");
  const panel = document.getElementById("chatPanel");
  const closeBtn = document.getElementById("chatClose");
  const form = document.getElementById("chatForm");
  const input = document.getElementById("chatInput");
  const sendBtn = document.getElementById("chatSend");
  const msgs = document.getElementById("chatMessages");

  const history = []; // [{role: "user"|"assistant", content: "..."}]

  function openPanel() {
    panel.classList.add("open");
    toggle.classList.add("hidden");
    setTimeout(() => input.focus(), 50);
  }
  function closePanel() {
    panel.classList.remove("open");
    toggle.classList.remove("hidden");
  }
  toggle.addEventListener("click", openPanel);
  closeBtn.addEventListener("click", closePanel);

  // sugerencias iniciales — clic prellena y envía
  msgs.querySelectorAll(".chatSuggest li").forEach((li) => {
    li.addEventListener("click", () => {
      input.value = li.dataset.q;
      form.requestSubmit();
    });
  });

  function appendMessage(role, text, extra) {
    const div = document.createElement("div");
    div.className = "chatMsg " + role;
    div.textContent = text;
    if (extra) {
      const meta = document.createElement("div");
      meta.className = "actions";
      meta.textContent = extra;
      div.appendChild(meta);
    }
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
    return div;
  }

  function showTyping() {
    const div = document.createElement("div");
    div.className = "chatTyping";
    div.textContent = "Pensando…";
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
    return div;
  }

  async function executeAction(action) {
    if (!action || !action.type) return null;
    switch (action.type) {
      case "fly_to":
        viewer.camera.flyTo({
          destination: Cesium.Cartesian3.fromDegrees(action.lon, action.lat, 15000),
          duration: 1.5,
        });
        return `volé a ${action.label || (action.lat + ", " + action.lon)}`;

      case "set_water_slider": {
        const slider = document.getElementById("waterSlider");
        const level = Math.max(0, Math.min(action.delta_h_m, parseFloat(slider.max)));
        slider.value = level;
        document.getElementById("waterSliderOut").textContent = level.toFixed(1) + " m";
        setManualLevel(viewer, level);
        return `nivel manual → ${level.toFixed(1)} m`;
      }

      case "set_rain_sim": {
        const slider = document.getElementById("rainSimSlider");
        const mm = Math.max(0, Math.min(action.mm, parseFloat(slider.max)));
        slider.value = mm;
        document.getElementById("rainSimOut").textContent = mm.toFixed(0) + " mm";
        await simulateRainfall(viewer, mm);
        return `lluvia simulada ${mm.toFixed(0)}mm (Δh ≈ ${action.delta_h_m?.toFixed?.(1) ?? "?"} m)`;
      }

      case "set_forecast_day": {
        const btn = document.querySelectorAll(".dayBtn")[action.dia - 1];
        if (btn) btn.click();
        return `pronóstico acumulado al día ${action.dia}`;
      }

      case "open_terrain_profile":
        openRiverModal(action.lat, action.lon, `Corte de terreno — ${action.label}`);
        return `corte 3D abierto en ${action.label}`;

      default:
        return null;
    }
  }

  async function sendMessage(text) {
    if (!text.trim()) return;
    appendMessage("user", text);
    history.push({ role: "user", content: text });

    input.value = "";
    input.disabled = true;
    sendBtn.disabled = true;
    const typing = showTyping();

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, history: history.slice(0, -1) }),
      });
      const data = await res.json();
      typing.remove();

      if (!res.ok) {
        const errMsg = data?.detail || `HTTP ${res.status}`;
        appendMessage("err", "⚠️ " + errMsg);
        history.pop(); // no persistimos el turno que falló
        return;
      }

      // ejecutar acciones ANTES de renderizar la respuesta, así el usuario ve
      // el mapa moverse mientras lee el texto
      const done = [];
      for (const a of data.actions || []) {
        const desc = await executeAction(a);
        if (desc) done.push(desc);
      }

      appendMessage("bot", data.reply, done.length ? "🛠 " + done.join(" · ") : null);
      history.push({ role: "assistant", content: data.reply });
    } catch (e) {
      typing.remove();
      appendMessage("err", "⚠️ No se pudo contactar al asistente: " + e.message);
      history.pop();
    } finally {
      input.disabled = false;
      sendBtn.disabled = false;
      input.focus();
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    sendMessage(input.value);
  });

  // Enter envía, Shift+Enter salta línea
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });
}

// --- Clima en vivo (satelital) ---
// Overlay único de nubes satelitales IR (NASA GIBS + NOAA GOES-East ABI
// Band13 Clean Infrared): imagen de tope de nubes en infrarrojo termal.
// Es la misma vista base de nubes que usan Meteored, Windy y los reportes
// meteorológicos operacionales — funciona día y noche.
//
// GIBS publica frames cada 10min con ~20-30min de latencia. Usamos:
//   - time slot "default" → último frame disponible (para el frame "ahora")
//   - timestamps ISO explícitos para los frames pasados del slider (2h atrás)
// Tile PNG con transparencia → Cesium lo drapea sobre el terreno 3D.
// Descartamos RainViewer satellite: su endpoint devuelve arrays vacíos con
// frecuencia. Descartamos GOES Air Mass RGB: útil pero no es la vista
// "de nubes" que pide la referencia de Meteored.

function initWeatherOverlays(viewer) {
  const toggleSat = document.getElementById("toggleSat");
  const opacityRow = document.getElementById("weatherOpacityRow");
  const opacitySlider = document.getElementById("weatherOpacity");
  const opacityOut = document.getElementById("weatherOpacityOut");
  const timeRow = document.getElementById("satTimeRow");
  const timeSlider = document.getElementById("satTime");
  const timeOut = document.getElementById("satTimeOut");
  const hint = document.getElementById("weatherHint");

  const GIBS_BASE = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best" +
                    "/GOES-East_ABI_Band13_Clean_Infrared/default";
  const GIBS_TAIL = "/GoogleMapsCompatible_Level6/{z}/{y}/{x}.png";
  const FRAMES_BACK = 12;  // slider cubre 12 pasos de 10min ≈ 2h
  const LATENCY_MIN = 30;  // el "ahora" real de GIBS Band13 va ~30min atrás

  const state = {
    satLayer: null,
    frames: [],       // [{iso, timeMs, label}]  idx 0 = más antiguo, N-1 = ahora
    frameIdx: 0,
    refreshTimer: null,
  };

  function updateVisibleControls() {
    const on = toggleSat.checked;
    opacityRow.style.display = on ? "" : "none";
    timeRow.style.display = on ? "" : "none";
    hint.style.display = on ? "" : "none";
  }

  function applyOpacity() {
    const a = parseFloat(opacitySlider.value);
    if (state.satLayer) state.satLayer.alpha = a;
    opacityOut.textContent = Math.round(a * 100) + "%";
  }
  opacitySlider.addEventListener("input", applyOpacity);

  function buildFrames() {
    // Reloj-alineamos a los slots de 10min y retrocedemos LATENCY_MIN para
    // caer en un frame que GIBS ya publicó.
    const anchor = new Date();
    anchor.setUTCSeconds(0, 0);
    anchor.setUTCMinutes(Math.floor(anchor.getUTCMinutes() / 10) * 10);
    anchor.setUTCMinutes(anchor.getUTCMinutes() - LATENCY_MIN);

    const frames = [];
    for (let i = FRAMES_BACK - 1; i >= 0; i--) {
      const t = new Date(anchor.getTime() - i * 10 * 60 * 1000);
      const iso = t.toISOString().replace(/\.\d+Z$/, "Z");
      frames.push({ iso, timeMs: t.getTime() });
    }
    state.frames = frames;
    timeSlider.max = frames.length - 1;
    timeSlider.value = frames.length - 1;   // "ahora" (el último disponible)
    state.frameIdx = frames.length - 1;
    updateTimeLabel();
  }

  function updateTimeLabel() {
    const f = state.frames[state.frameIdx];
    if (!f) { timeOut.textContent = "—"; return; }
    const isLatest = state.frameIdx === state.frames.length - 1;
    if (isLatest) {
      timeOut.textContent = "ahora (últ. disponible)";
      return;
    }
    const diffMin = Math.round((f.timeMs - Date.now()) / 60000);
    timeOut.textContent = diffMin + " min";
  }

  function buildSatProvider() {
    // Para el último frame usamos "default" (deja que GIBS resuelva); para
    // frames pasados, timestamp explícito. Cache-buster por bin de 10min.
    const isLatest = state.frameIdx === state.frames.length - 1;
    const timeSlot = isLatest ? "default" : state.frames[state.frameIdx].iso;
    const bin10min = Math.floor(Date.now() / (10 * 60 * 1000));
    const url = `${GIBS_BASE}/${timeSlot}${GIBS_TAIL}?v=${bin10min}`;
    return new Cesium.UrlTemplateImageryProvider({
      url,
      maximumLevel: 6,
      credit: "Nubes IR © NASA GIBS / NOAA GOES-East (ABI Band 13)",
    });
  }

  function mountSat() {
    if (state.satLayer) {
      viewer.imageryLayers.remove(state.satLayer, true);
      state.satLayer = null;
    }
    if (!toggleSat.checked) return;
    state.satLayer = viewer.imageryLayers.addImageryProvider(buildSatProvider());
    state.satLayer.alpha = parseFloat(opacitySlider.value);
  }

  function scheduleAutoRefresh() {
    if (state.refreshTimer || !toggleSat.checked) return;
    state.refreshTimer = setInterval(() => {
      if (!toggleSat.checked) return;
      const wasLatest = state.frameIdx === state.frames.length - 1;
      buildFrames();
      if (wasLatest) state.frameIdx = state.frames.length - 1;
      timeSlider.value = state.frameIdx;
      mountSat();
    }, 5 * 60 * 1000);
  }

  // Partículas de viento (canvas overlay) — módulo aparte, ver createWindParticles()
  const wind = createWindParticles(viewer);
  window.__wind = wind;   // debug / consola
  const toggleWind = document.getElementById("toggleWind");
  toggleWind.addEventListener("change", async () => {
    if (toggleWind.checked) {
      await wind.start();
      // Al arrancar, sincronizar con la selección satelital si está activa
      if (toggleSat.checked && state.frames[state.frameIdx]) {
        wind.setTime(state.frames[state.frameIdx].timeMs);
      }
    } else {
      wind.stop();
    }
  });

  function syncWindToSatFrame() {
    if (!toggleSat.checked) return;
    const f = state.frames[state.frameIdx];
    if (!f) return;
    wind.setTime(f.timeMs);
  }

  toggleSat.addEventListener("change", () => {
    updateVisibleControls();
    if (toggleSat.checked && state.frames.length === 0) buildFrames();
    mountSat();
    scheduleAutoRefresh();
    syncWindToSatFrame();
  });
  timeSlider.addEventListener("input", () => {
    state.frameIdx = parseInt(timeSlider.value, 10);
    updateTimeLabel();
    if (toggleSat.checked) mountSat();
    syncWindToSatFrame();
  });
}

// --- Partículas de viento estilo Meteored/nullschool ---
// Canvas 2D absoluto sobre el globo. Cada frame:
//   1. Fade oscuro traslúcido -> deja "estela" que decae.
//   2. Para cada partícula: interp bilineal de u,v en la grilla de Open-Meteo,
//      avanza en lat/lon, proyecta a pixel con viewer.scene.cartesianToCanvasCoordinates.
//   3. Si sale de pantalla o cumple maxAge, respawnea dentro del rectángulo
//      visible actual (así al hacer zoom no queda vacío).
// La grilla la sirve /api/wind_grid (backend/wind.py), refresco 30min.

function createWindParticles(viewer) {
  const canvas = document.getElementById("windCanvas");
  const ctx = canvas.getContext("2d");
  const state = {
    running: false,
    grid: null,          // {nx, ny, bbox, frames: [{time, u, v, speed, speed_max, timeMs}]}
    frameIdx: 0,         // frame activo dentro de grid.frames
    particles: [],
    rafId: null,
    lastFrameMs: 0,
    refreshTimer: null,
    dpr: window.devicePixelRatio || 1,
  };

  const PARTICLE_COUNT = 900;
  const MAX_AGE_FRAMES = 90;
  const FADE_ALPHA = 0.965;
  // Objetivo: la partícula MÁS rápida se mueve ~PX_PER_FRAME_MAX px/frame.
  // Convertimos ese objetivo a un multiplicador dinámico según el zoom actual
  // (grados de latitud por pixel), así funciona igual regional y de cerca.
  const PX_PER_FRAME_MAX = 1.8;
  const LINE_WIDTH = 1.3;

  function resizeCanvas() {
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    const dpr = state.dpr;
    if (canvas.width !== Math.floor(w * dpr) || canvas.height !== Math.floor(h * dpr)) {
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
    }
  }

  function visibleRectDeg() {
    const rect = viewer.camera.computeViewRectangle();
    if (!rect) {
      const b = state.grid.bbox;
      return { west: b.west, east: b.east, south: b.south, north: b.north };
    }
    return {
      west: Cesium.Math.toDegrees(rect.west),
      east: Cesium.Math.toDegrees(rect.east),
      south: Cesium.Math.toDegrees(rect.south),
      north: Cesium.Math.toDegrees(rect.north),
    };
  }

  function clampToGrid(lon, lat) {
    const b = state.grid.bbox;
    return {
      lon: Math.min(Math.max(lon, b.west), b.east),
      lat: Math.min(Math.max(lat, b.south), b.north),
    };
  }

  function sampleWind(lat, lon) {
    const g = state.grid;
    const b = g.bbox;
    const frame = g.frames[state.frameIdx];
    const c = clampToGrid(lon, lat);
    const fx = (c.lon - b.west) / (b.east - b.west) * (g.nx - 1);
    const fy = (c.lat - b.south) / (b.north - b.south) * (g.ny - 1);
    const x0 = Math.floor(fx), y0 = Math.floor(fy);
    const x1 = Math.min(x0 + 1, g.nx - 1), y1 = Math.min(y0 + 1, g.ny - 1);
    const tx = fx - x0, ty = fy - y0;
    const idx = (r, c2) => r * g.nx + c2;
    const uu = frame.u, vv = frame.v;
    const u00 = uu[idx(y0, x0)], u10 = uu[idx(y0, x1)];
    const u01 = uu[idx(y1, x0)], u11 = uu[idx(y1, x1)];
    const v00 = vv[idx(y0, x0)], v10 = vv[idx(y0, x1)];
    const v01 = vv[idx(y1, x0)], v11 = vv[idx(y1, x1)];
    const u = (u00 * (1 - tx) + u10 * tx) * (1 - ty) + (u01 * (1 - tx) + u11 * tx) * ty;
    const v = (v00 * (1 - tx) + v10 * tx) * (1 - ty) + (v01 * (1 - tx) + v11 * tx) * ty;
    return { u, v, speed: Math.hypot(u, v) };
  }

  function randInRect(rect) {
    return {
      lon: rect.west + Math.random() * (rect.east - rect.west),
      lat: rect.south + Math.random() * (rect.north - rect.south),
      age: Math.floor(Math.random() * MAX_AGE_FRAMES),
    };
  }

  function intersectWithGridBbox(rect) {
    const b = state.grid.bbox;
    return {
      west: Math.max(rect.west, b.west),
      east: Math.min(rect.east, b.east),
      south: Math.max(rect.south, b.south),
      north: Math.min(rect.north, b.north),
    };
  }

  function spawnParticles() {
    const rect = intersectWithGridBbox(visibleRectDeg());
    const useRect = (rect.east > rect.west && rect.north > rect.south) ? rect : state.grid.bbox;
    state.particles = new Array(PARTICLE_COUNT).fill(0).map(() => randInRect(useRect));
  }

  function colorForSpeed(speed) {
    const frameMax = state.grid.frames[state.frameIdx].speed_max;
    const t = Math.min(speed / Math.max(frameMax, 3), 1);
    if (t < 0.25) return "rgba(180, 220, 255, 0.75)";
    if (t < 0.5)  return "rgba(120, 200, 255, 0.85)";
    if (t < 0.75) return "rgba(255, 240, 150, 0.90)";
    return "rgba(255, 120, 90, 0.95)";
  }

  const _scratchCartesian = new Cesium.Cartesian3();
  function projectToScreen(lon, lat) {
    // Nota: NO reutilizar un scratch Cartesian2 acá — el step() llama a esta
    // función dos veces por partícula (pFrom y pTo) antes de leer las coords,
    // así que ambos tienen que ser objetos distintos.
    Cesium.Cartesian3.fromDegrees(lon, lat, 0, Cesium.Ellipsoid.WGS84, _scratchCartesian);
    return viewer.scene.cartesianToCanvasCoordinates(_scratchCartesian, new Cesium.Cartesian2());
  }

  function step(nowMs) {
    if (!state.running) return;
    const dt = Math.min((nowMs - state.lastFrameMs) / 1000, 0.1);
    state.lastFrameMs = nowMs;

    resizeCanvas();
    const w = canvas.clientWidth, h = canvas.clientHeight;

    // Fade con destination-in preserva transparencia (no cae fondo negro sobre el mapa)
    ctx.globalCompositeOperation = "destination-in";
    ctx.fillStyle = "rgba(0,0,0," + FADE_ALPHA + ")";
    ctx.fillRect(0, 0, w, h);
    ctx.globalCompositeOperation = "source-over";

    ctx.lineWidth = LINE_WIDTH;
    const visRect = intersectWithGridBbox(visibleRectDeg());
    const visArea = (visRect.east - visRect.west) * (visRect.north - visRect.south);
    const gridArea = (state.grid.bbox.east - state.grid.bbox.west) * (state.grid.bbox.north - state.grid.bbox.south);
    const inGrid = visArea > 0 && visArea < gridArea * 4;

    // Multiplicador dinámico: convertir el objetivo (px/frame) a metros/segundo
    // scale-equivalente en el zoom actual. degPerPx = ancho_deg / ancho_px.
    // metros/deg ≈ 111320. Queremos: speed_max * dt * MULT / metrosPorDeg = degPerPx * PX_TARGET.
    const degPerPx = (visRect.east - visRect.west) / Math.max(w, 1);
    const targetDeg = degPerPx * PX_PER_FRAME_MAX;
    const speedMax = Math.max(state.grid.frames[state.frameIdx].speed_max, 3);
    const MULT = Math.max(50, targetDeg * 111320 / (speedMax * Math.max(dt, 0.001)));

    for (let i = 0; i < state.particles.length; i++) {
      const p = state.particles[i];
      p.age++;
      const inside =
        p.lon >= visRect.west && p.lon <= visRect.east &&
        p.lat >= visRect.south && p.lat <= visRect.north;
      if (!inGrid || !inside || p.age > MAX_AGE_FRAMES) {
        const useRect = visArea > 0 ? visRect : state.grid.bbox;
        Object.assign(p, randInRect(useRect), { age: 0 });
        continue;
      }

      const wind = sampleWind(p.lat, p.lon);
      const dLat = wind.v * dt * MULT / 111320;
      const dLon = wind.u * dt * MULT / (111320 * Math.cos(p.lat * Math.PI / 180));
      const newLon = p.lon + dLon, newLat = p.lat + dLat;

      const pFrom = projectToScreen(p.lon, p.lat);
      const pTo = projectToScreen(newLon, newLat);
      if (pFrom && pTo) {
        ctx.strokeStyle = colorForSpeed(wind.speed);
        ctx.beginPath();
        ctx.moveTo(pFrom.x, pFrom.y);
        ctx.lineTo(pTo.x, pTo.y);
        ctx.stroke();
      }
      p.lon = newLon;
      p.lat = newLat;
    }

    state.rafId = requestAnimationFrame(step);
  }

  async function fetchGrid() {
    const res = await fetch("/api/wind_grid");
    if (!res.ok) throw new Error("HTTP " + res.status);
    const g = await res.json();
    // Normalizar timestamps a epoch ms para comparar rápido con el slider satelital.
    g.frames.forEach((f) => { f.timeMs = new Date(f.time).getTime(); });
    state.grid = g;
    state.frameIdx = g.frames.length - 1; // por defecto, frame más nuevo
  }

  function setTime(targetMs) {
    if (!state.grid || !state.grid.frames.length) return;
    // Elegimos el frame cuyo timeMs esté más cerca del targetMs.
    let bestIdx = 0, bestDiff = Infinity;
    for (let i = 0; i < state.grid.frames.length; i++) {
      const d = Math.abs(state.grid.frames[i].timeMs - targetMs);
      if (d < bestDiff) { bestDiff = d; bestIdx = i; }
    }
    state.frameIdx = bestIdx;
  }

  async function start() {
    if (state.running) return;
    try {
      if (!state.grid) await fetchGrid();
    } catch (e) {
      console.warn("No se pudo cargar wind_grid:", e);
      alert("No se pudo cargar la grilla de viento. Ver consola.");
      document.getElementById("toggleWind").checked = false;
      return;
    }
    canvas.classList.add("active");
    resizeCanvas();
    spawnParticles();
    state.running = true;
    state.lastFrameMs = performance.now();
    state.rafId = requestAnimationFrame(step);
    if (!state.refreshTimer) {
      state.refreshTimer = setInterval(() => fetchGrid().catch(() => {}), 30 * 60 * 1000);
    }
    if (!state._removeMoveEnd) {
      state._removeMoveEnd = viewer.camera.moveEnd.addEventListener(() => {
        if (!state.running) return;
        const rect = intersectWithGridBbox(visibleRectDeg());
        if (rect.east <= rect.west || rect.north <= rect.south) return;
        for (let i = 0; i < state.particles.length; i += 4) {
          Object.assign(state.particles[i], randInRect(rect), { age: 0 });
        }
      });
    }
  }

  function stop() {
    state.running = false;
    if (state.rafId) cancelAnimationFrame(state.rafId);
    state.rafId = null;
    ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
    canvas.classList.remove("active");
  }

  return {
    start, stop, setTime,
    isRunning: () => state.running,
    // Introspección para debug / tests: nunca mutar desde afuera.
    getActiveFrame: () => (state.grid ? {
      idx: state.frameIdx,
      count: state.grid.frames.length,
      time: state.grid.frames[state.frameIdx]?.time,
      timeMs: state.grid.frames[state.frameIdx]?.timeMs,
      speedMax: state.grid.frames[state.frameIdx]?.speed_max,
    } : null),
  };
}

// --- Relaves mineros ---
// Dos partes:
//   1) Capa Cesium: cada relave es un punto coloreado por estado
//      (dormido gris / en riesgo amarillo / vertiendo rojo). Etiqueta con
//      nombre al pasar el mouse. Se carga bajo demanda al activar el toggle.
//   2) Canvas overlay (#relavesCanvas): partículas naranja-óxido que fluyen
//      desde cada relave "vertiendo" por su polilínea `downstream_path`
//      precalculada por scripts/11_fetch_relaves.py. Misma técnica de trazo
//      con estela que el módulo de viento.
//
// Trigger de "vertido": se decide en el frontend a partir del último flood
// state cargado (delta_h_m y rainfall_mm_24h — window.__lastFloodData).
// El relave vierte si:
//   - riesgo_geometrico == "alto" (dist al río < 500m) Y delta_h > 2m, o
//   - riesgo_geometrico != "bajo" Y rainfall > 80 mm/24h, o
//   - delta_h > 6m (crecida mayor: TODOS los cercanos al río vierten)
// El módulo se re-evalúa cada vez que window.__lastFloodData cambia.

function createRelavesModule(viewer) {
  const canvas = document.getElementById("relavesCanvas");
  const ctx = canvas.getContext("2d");

  const state = {
    running: false,
    data: null,               // GeoJSON completo
    features: [],             // features cache
    entities: [],             // Cesium entities creadas
    dataSource: null,         // DataSource contenedor
    rafId: null,
    lastFrameMs: 0,
    dpr: window.devicePixelRatio || 1,
    // Partículas activas: [{lon, lat, pathIdx, segT, age, feature}]
    particles: [],
  };

  // Cantidad de partículas por relave vertiendo. Con ~250-300 relaves
  // vertiendo en una crecida grande, 120 partículas c/u = ~30-36k trazos por
  // frame — canvas 2D lo aguanta con lineWidth chica en desktop moderno.
  const PARTICLES_PER_SPILL = 120;
  const MAX_AGE_FRAMES = 140;
  const FADE_ALPHA = 0.94;
  const LINE_WIDTH = 1.6;
  const SPILL_STEP_FRAC = 0.0025;   // base — se acelera con delta_h en step()
  // Jitter lateral en grados (~0.0004° ≈ 45m a lat -30°). Da anchura de pluma
  // en vez de una línea de 1px. Cada partícula tiene su offset perpendicular
  // constante durante su vida.
  const LATERAL_JITTER_DEG = 0.0004;

  function resizeCanvas() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    const dpr = state.dpr;
    if (canvas.width !== Math.floor(w * dpr) || canvas.height !== Math.floor(h * dpr)) {
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
    }
  }

  function statusOf(feature) {
    const p = feature.properties;
    const flood = window.__lastFloodData;
    if (!flood) return "dormido";
    const dh = flood.delta_h_m || 0;
    // rainfall_mm_24h en modo auto/simulated_rainfall,
    // cumulative_rainfall_mm en modo forecast (acumulado a N días).
    const rain = flood.rainfall_mm_24h || flood.cumulative_rainfall_mm || 0;
    if (p.riesgo_geometrico === "alto" && dh > 2) return "vertiendo";
    if (p.riesgo_geometrico !== "bajo" && rain > 80) return "vertiendo";
    if (dh > 6 && p.riesgo_geometrico !== "bajo") return "vertiendo";
    // marca "en riesgo" (aún no vierte) si condiciones se aproximan
    if (p.riesgo_geometrico === "alto" && (dh > 0.5 || rain > 40)) return "en_riesgo";
    if (p.riesgo_geometrico !== "bajo" && rain > 60) return "en_riesgo";
    return "dormido";
  }

  const COLORS = {
    dormido:   Cesium.Color.fromCssColorString("#8a8a8a"),
    en_riesgo: Cesium.Color.fromCssColorString("#f4d03f"),
    vertiendo: Cesium.Color.fromCssColorString("#e74c3c"),
  };

  async function ensureLoaded() {
    if (state.data) return true;
    const res = await fetch("/api/relaves");
    if (!res.ok) {
      console.warn("Relaves no disponibles (HTTP " + res.status + "). ¿Corriste scripts/11_fetch_relaves.py?");
      return false;
    }
    state.data = await res.json();
    state.features = state.data.features || [];
    state.dataSource = new Cesium.CustomDataSource("relaves");
    viewer.dataSources.add(state.dataSource);
    for (const f of state.features) {
      const [lon, lat] = f.geometry.coordinates;
      const p = f.properties;
      const status = statusOf(f);
      const ent = state.dataSource.entities.add({
        position: Cesium.Cartesian3.fromDegrees(lon, lat),
        point: {
          pixelSize: p.riesgo_geometrico === "alto" ? 9 : 6,
          color: COLORS[status],
          outlineColor: Cesium.Color.BLACK,
          outlineWidth: 1,
          heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
        description:
          `<b>${escapeHtml(p.nombre)}</b><br>` +
          (p.empresa ? `Empresa: ${escapeHtml(p.empresa)}<br>` : "") +
          (p.comuna ? `Comuna: ${escapeHtml(p.comuna)}<br>` : "") +
          (p.tipo ? `Tipo: ${escapeHtml(p.tipo)}<br>` : "") +
          (p.estado ? `Estado: ${escapeHtml(p.estado)}<br>` : "") +
          `Río cercano: ${escapeHtml(p.rio_cercano || "—")} (${Math.round(p.dist_rio_m)}m)<br>` +
          `Riesgo geométrico: <b>${p.riesgo_geometrico}</b>`,
      });
      ent._relave = f;
      state.entities.push(ent);
    }
    return true;
  }

  function refreshStatusColors() {
    for (const ent of state.entities) {
      const s = statusOf(ent._relave);
      ent.point.color = COLORS[s];
    }
    seedParticles();
  }

  function seedParticles() {
    // Reconstruye la lista de partículas activas por relave vertiendo,
    // escalonadas en globalT para flujo continuo, con offset lateral
    // aleatorio (jitter) para simular una pluma de material y no una línea.
    const spilling = state.features.filter(f =>
      statusOf(f) === "vertiendo" &&
      f.properties.downstream_path &&
      f.properties.downstream_path.length >= 2
    );
    const out = [];
    for (const f of spilling) {
      for (let i = 0; i < PARTICLES_PER_SPILL; i++) {
        out.push({
          feature: f,
          globalT: Math.random(),                          // fase random en el path
          // offset lateral en unidades normalizadas [-1..1] * LATERAL_JITTER_DEG
          jitter: (Math.random() * 2 - 1),
          age: Math.floor(Math.random() * MAX_AGE_FRAMES),
        });
      }
    }
    state.particles = out;
  }

  // Convierte (segIdx, segT) en un punto del path CON offset lateral aplicado
  // (perpendicular al segmento). El signo/magnitud del offset viene de p.jitter.
  function pointOnPath(path, segIdx, segT, jitterAmt) {
    const a = path[segIdx], b = path[segIdx + 1];
    const lon = a[0] + (b[0] - a[0]) * segT;
    const lat = a[1] + (b[1] - a[1]) * segT;
    if (!jitterAmt) return [lon, lat];
    const dx = b[0] - a[0], dy = b[1] - a[1];
    const len = Math.hypot(dx, dy) || 1;
    // perpendicular unitario (normal 2D) — luego escala por LATERAL_JITTER_DEG
    const nx = -dy / len, ny = dx / len;
    return [lon + nx * jitterAmt, lat + ny * jitterAmt];
  }

  const _scratchCart = new Cesium.Cartesian3();
  function project(lon, lat) {
    Cesium.Cartesian3.fromDegrees(lon, lat, 0, Cesium.Ellipsoid.WGS84, _scratchCart);
    return viewer.scene.cartesianToCanvasCoordinates(_scratchCart, new Cesium.Cartesian2());
  }

  function step(nowMs) {
    if (!state.running) return;
    state.lastFrameMs = nowMs;
    resizeCanvas();
    const w = canvas.clientWidth, h = canvas.clientHeight;

    ctx.globalCompositeOperation = "destination-in";
    ctx.fillStyle = "rgba(0,0,0," + FADE_ALPHA + ")";
    ctx.fillRect(0, 0, w, h);
    ctx.globalCompositeOperation = "source-over";
    ctx.lineWidth = LINE_WIDTH;

    // Velocidad de arrastre escala con la magnitud de la crecida — río más
    // grande = corriente más rápida = partículas viajan más rápido corriente
    // abajo. Rango 0.6x..3x sobre SPILL_STEP_FRAC.
    const flood = window.__lastFloodData;
    const dh = flood?.delta_h_m || 0;
    const rain = flood?.rainfall_mm_24h || flood?.cumulative_rainfall_mm || 0;
    // usar el mayor "driver" para escalar (crecida o temporal)
    const intensity = Math.max(dh / 3, rain / 80);
    const stepScale = Math.max(0.6, Math.min(intensity, 3.0));
    const step_frac = SPILL_STEP_FRAC * stepScale;

    for (let i = 0; i < state.particles.length; i++) {
      const p = state.particles[i];
      const path = p.feature.properties.downstream_path;
      if (!path || path.length < 2) continue;
      p.age++;
      p.globalT += step_frac;
      if (p.age > MAX_AGE_FRAMES || p.globalT >= 1) {
        // Respawnear cerca del origen con jitter nuevo — así la pluma se
        // regenera con distribución fresca en vez de trazos repetidos.
        p.age = 0;
        p.globalT = Math.random() * 0.08;
        p.jitter = (Math.random() * 2 - 1);
      }

      const nSegs = path.length - 1;
      const total = p.globalT * nSegs;
      const segIdx = Math.min(Math.floor(total), nSegs - 1);
      const segT = total - segIdx;
      const prevT = Math.max(0, p.globalT - step_frac * 2);
      const totalPrev = prevT * nSegs;
      const segIdxPrev = Math.min(Math.floor(totalPrev), nSegs - 1);
      const segTPrev = totalPrev - segIdxPrev;

      // Fade lateral: jitter atenuado al principio para que salgan del punto
      // exacto del relave y se abran corriente abajo (parece una pluma real).
      const jitterHere = p.jitter * LATERAL_JITTER_DEG * Math.min(1, p.globalT * 4);
      const jitterPrev = p.jitter * LATERAL_JITTER_DEG * Math.min(1, prevT * 4);

      const [lon0, lat0] = pointOnPath(path, segIdx, segT, jitterHere);
      const [lonPrev, latPrev] = pointOnPath(path, segIdxPrev, segTPrev, jitterPrev);

      const pFrom = project(lonPrev, latPrev);
      const pTo = project(lon0, lat0);
      if (pFrom && pTo) {
        // Gradiente de color: naranja intenso cerca del origen, marrón óxido
        // más apagado corriente abajo (el material se dispersa/diluye).
        // r: 240 → 130, g: 130 → 55, b: 40 → 20
        const t = p.globalT;
        const r = Math.round(240 - 110 * t);
        const g = Math.round(130 - 75 * t);
        const b = Math.round(40 - 20 * t);
        const alpha = 0.65 + 0.30 * (1 - p.age / MAX_AGE_FRAMES);
        ctx.strokeStyle = `rgba(${r}, ${g}, ${b}, ${alpha.toFixed(2)})`;
        ctx.beginPath();
        ctx.moveTo(pFrom.x, pFrom.y);
        ctx.lineTo(pTo.x, pTo.y);
        ctx.stroke();
      }
    }

    state.rafId = requestAnimationFrame(step);
  }

  async function start() {
    if (state.running) return;
    const ok = await ensureLoaded();
    if (!ok) {
      document.getElementById("toggleRelaves").checked = false;
      alert("No hay data de relaves. Corre 'python scripts/11_fetch_relaves.py' desde la raíz del proyecto para descargar el catastro SERNAGEOMIN.");
      return;
    }
    if (state.dataSource) state.dataSource.show = true;
    refreshStatusColors();
    canvas.classList.add("active");
    resizeCanvas();
    state.running = true;
    state.lastFrameMs = performance.now();
    state.rafId = requestAnimationFrame(step);
  }

  function stop() {
    state.running = false;
    if (state.rafId) cancelAnimationFrame(state.rafId);
    state.rafId = null;
    ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
    canvas.classList.remove("active");
    if (state.dataSource) state.dataSource.show = false;
  }

  // API pública
  return {
    start, stop,
    onFloodStateChanged: () => { if (state.running) refreshStatusColors(); },
    getStats: () => {
      if (!state.features.length) return null;
      const counts = { dormido: 0, en_riesgo: 0, vertiendo: 0 };
      state.features.forEach(f => counts[statusOf(f)]++);
      return { total: state.features.length, ...counts };
    },
  };
}
