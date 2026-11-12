"""Smoke test de la API: las rutas principales responden con datos en disco."""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.app import app

client = TestClient(app)


def test_frontend_se_sirve():
    r = client.get("/")
    assert r.status_code == 200
    assert "Simulador" in r.text


def test_flood_manual_responde():
    if not (ROOT / "data" / "flood" / "hand.tif").exists():
        pytest.skip("HAND no generado")
    r = client.get("/api/flood?delta_h=2")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "manual"
    assert body["delta_h_m"] == 2
    assert body["flooded_area_km2"] > 0
    assert "geojson" in body


def test_capas_estaticas():
    for ruta in ["/api/basin", "/api/markers", "/api/waterways", "/api/crossings"]:
        r = client.get(ruta)
        assert r.status_code in (200, 404), ruta  # 404 solo si el dato no está generado
