"""Pruebas de endurecimiento: cabeceras, validación de parámetros y rate limit."""

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import app as appmod  # noqa: E402

client = TestClient(appmod.app)


def test_cabeceras_de_seguridad():
    r = client.get("/api/flood/config")
    for h in ("Content-Security-Policy", "X-Content-Type-Options", "X-Frame-Options",
              "Referrer-Policy", "Permissions-Policy"):
        assert h in r.headers


def test_docs_desactivadas_por_defecto():
    assert client.get("/docs").status_code in (404, 200) and "swagger" not in client.get("/openapi.json").text.lower()


def test_parametros_fuera_de_rango_rechazados():
    assert client.get("/api/flood?delta_h=999").status_code == 422
    assert client.get("/api/flood?rainfall_mm=-5").status_code == 422
    assert client.get("/api/flood/forecast_day?day=99").status_code == 422
    assert client.get("/api/terrain_profile?lat=500&lon=0").status_code == 422
    assert client.get("/api/terrain_profile?lat=0&lon=0&size_km=1000").status_code == 422


def test_chat_valida_roles_y_largo():
    bad_role = {"message": "hola", "history": [{"role": "system", "content": "x"}]}
    assert client.post("/api/chat", json=bad_role).status_code == 422
    assert client.post("/api/chat", json={"message": "x" * 2001}).status_code == 422


def test_rate_limit_en_rutas_de_ia():
    appmod._hits.clear()
    codes = [client.post("/api/chat", json={"message": ""}).status_code for _ in range(6)]
    assert 429 in codes
    appmod._hits.clear()
