"""Tests de la lógica numérica pura (sin red, con estado sintético)."""

import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from backend import flood
from backend.rain_history import _compute_stats

process_terrain = __import__("03_process_terrain")


# --- flood: bisección volumen <-> nivel sobre HAND sintético ---


@pytest.fixture()
def synthetic_hand(monkeypatch):
    """HAND sintético 10x10: valores 0..9 por fila, celdas de 100x100m."""
    hand = np.tile(np.arange(10, dtype=float).reshape(-1, 1), (1, 10))
    state = {
        "hand": hand,
        "valid": np.ones_like(hand, dtype=bool),
        "cell_area_m2": 100.0 * 100.0,
        "basin_area_m2": float(hand.size) * 100.0 * 100.0,
        "transform": from_origin(0, 1000, 100, 100),
        "crs": "EPSG:32719",
    }
    monkeypatch.setattr(flood, "_state", state)
    return state


def test_delta_h_cero_para_volumen_cero(synthetic_hand):
    delta_h, capped = flood._solve_delta_h_for_volume(0.0)
    assert delta_h == 0.0
    assert capped is False


def test_delta_h_monotono_con_volumen(synthetic_hand):
    v1, _ = flood._solve_delta_h_for_volume(1e5)
    v2, _ = flood._solve_delta_h_for_volume(1e6)
    v3, _ = flood._solve_delta_h_for_volume(5e6)
    assert 0 < v1 < v2 < v3


def test_delta_h_tope_con_volumen_imposible(synthetic_hand):
    delta_h, capped = flood._solve_delta_h_for_volume(1e15)
    assert delta_h == flood.MAX_DELTA_H_M
    assert capped is True


def test_volumen_consistente_con_nivel(synthetic_hand):
    """El volumen contenido al nivel resuelto debe igualar el pedido (±5%)."""
    target = 2e6
    delta_h, _ = flood._solve_delta_h_for_volume(target)
    vol = flood._flood_volume(delta_h)
    assert abs(vol - target) / target < flood.BISECTION_TOLERANCE + 0.01


def test_flood_mask_crece_con_nivel(synthetic_hand):
    m1 = flood._flood_mask(1.0).sum()
    m5 = flood._flood_mask(5.0).sum()
    assert m1 < m5
    assert m1 == 20  # filas con HAND 0 y 1 (2 filas x 10 celdas)


# --- rain_history: percentiles y récords ---


def test_compute_stats_basico():
    dates = [f"2020-01-{d:02d}" for d in range(1, 11)]
    precip = [0, 0, 5, 0, 50, 0, 10, 0, 0, 100]
    stats = _compute_stats(dates, precip)
    assert stats["max_diario_mm"] == 100
    assert stats["top_10_dias_mas_lluviosos"][0]["fecha"] == "2020-01-10"
    assert stats["dias_con_mas_de_50mm_total_periodo"] == 2  # 50 y 100
    assert stats["promedio_anual_mm"] == pytest.approx(165.0)


def test_compute_stats_maneja_none():
    stats = _compute_stats(["2020-01-01", "2020-01-02"], [None, 20])
    assert stats["max_diario_mm"] == 20


# --- 03_process_terrain.sample_grid: continuidad de bordes entre teselas ---


def _memory_dataset(width=200, height=200, res=0.01):
    """Raster sintético suave en memoria (gradiente + seno)."""
    x = np.linspace(0, 4 * np.pi, width)
    y = np.linspace(0, 4 * np.pi, height)
    data = (np.sin(x)[None, :] * 100 + np.cos(y)[:, None] * 100 + 500).astype("float32")
    transform = from_origin(-71.0, -30.0, res, res)
    memfile = MemoryFile()
    with memfile.open(
        driver="GTiff", height=height, width=width, count=1,
        dtype="float32", crs="EPSG:4326", transform=transform,
    ) as dst:
        dst.write(data, 1)
    return memfile


def test_sample_grid_bordes_identicos_entre_teselas_vecinas():
    """El borde este de una tesela debe coincidir con el borde oeste de su
    vecina — si no, el terreno 3D muestra grietas. Se tolera ruido de float32
    (<1mm): el encoder quantized-mesh cuantiza mucho más grueso que eso."""
    memfile = _memory_dataset()
    with memfile.open() as ds:
        west, south, east, north = -70.9, -31.5, -70.5, -30.1
        mid = (west + east) / 2
        size = 65
        left = process_terrain.sample_grid(ds, west, south, mid, north, size)
        right = process_terrain.sample_grid(ds, mid, south, east, north, size)
        assert np.abs(left[:, -1] - right[:, 0]).max() < 1e-3


def test_sample_grid_valores_dentro_del_rango_del_raster():
    memfile = _memory_dataset()
    with memfile.open() as ds:
        grid = process_terrain.sample_grid(ds, -70.9, -31.5, -70.5, -30.1, 65)
        assert grid.shape == (65, 65)
        # bilineal nunca extrapola fuera del rango de los datos
        assert grid.min() >= 300 - 1e-3
        assert grid.max() <= 700 + 1e-3
