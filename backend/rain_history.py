"""
Registros históricos de lluvia diaria para la cuenca del Limarí, desde el
archivo histórico de Open-Meteo (reanálisis ERA5, ~25 años de datos diarios).

Se usa como contexto para el asesor IA (backend/ai_advisor.py): percentiles
de lluvia diaria, los días más lluviosos registrados y promedios anuales,
para poder comparar la lluvia actual/pronosticada contra la historia real
de la zona en vez de opinar en el vacío.

Limitación honesta: es reanálisis de modelo en UN punto (Ovalle), no una
red de estaciones pluviométricas de la DGA — sirve para orden de magnitud
y percentiles, no para récords oficiales.
"""

import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "data" / "hydro" / "rain_history_stats.json"

# Ovalle (misma coordenada de referencia que markers.json — no verificada
# contra fuente oficial, ver README)
LAT, LON = -30.6, -71.2
START_DATE = "2000-01-01"

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def _compute_stats(dates: list[str], precip: list[float]) -> dict:
    arr = np.array([p if p is not None else 0.0 for p in precip], dtype=float)
    dates_arr = np.array(dates)

    rainy = arr[arr >= 1.0]  # días con lluvia apreciable (>=1mm)
    top_idx = np.argsort(arr)[::-1][:10]
    top_days = [
        {"fecha": str(dates_arr[i]), "mm": round(float(arr[i]), 1)} for i in top_idx
    ]

    years = {}
    for d, p in zip(dates, arr):
        years.setdefault(d[:4], 0.0)
        years[d[:4]] += p
    annual_totals = list(years.values())

    return {
        "fuente": "Open-Meteo archive (reanálisis ERA5), punto único en Ovalle — no estaciones DGA",
        "periodo": f"{dates[0]} a {dates[-1]}",
        "punto": {"lat": LAT, "lon": LON, "nombre": "Ovalle"},
        "dias_totales": int(len(arr)),
        "promedio_anual_mm": round(float(np.mean(annual_totals)), 1),
        "p90_dia_lluvioso_mm": round(float(np.percentile(rainy, 90)), 1) if len(rainy) else 0,
        "p95_dia_lluvioso_mm": round(float(np.percentile(rainy, 95)), 1) if len(rainy) else 0,
        "p99_dia_lluvioso_mm": round(float(np.percentile(rainy, 99)), 1) if len(rainy) else 0,
        "max_diario_mm": round(float(arr.max()), 1),
        "dias_con_mas_de_20mm_por_anio": round(float((arr >= 20).sum() / max(len(years), 1)), 1),
        "dias_con_mas_de_50mm_total_periodo": int((arr >= 50).sum()),
        "top_10_dias_mas_lluviosos": top_days,
    }


def get_history_stats(force: bool = False) -> dict:
    """Estadísticas históricas de lluvia diaria (cacheadas en disco: el
    archivo histórico no cambia hacia atrás, se refresca solo con force)."""
    if CACHE_PATH.exists() and not force:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))

    end = date.today() - timedelta(days=8)  # el archivo tiene ~1 semana de rezago
    resp = requests.get(
        ARCHIVE_URL,
        params={
            "latitude": LAT,
            "longitude": LON,
            "start_date": START_DATE,
            "end_date": end.isoformat(),
            "daily": "precipitation_sum",
            "timezone": "auto",
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    stats = _compute_stats(data["daily"]["time"], data["daily"]["precipitation_sum"])

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats


if __name__ == "__main__":
    print(json.dumps(get_history_stats(force=True), ensure_ascii=False, indent=2))
