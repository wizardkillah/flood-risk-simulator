"""Regresiones de endurecimiento: IP real tras el proxy, poda de memoria,
coherencia CSP/HTML y piso del parámetro refresh."""

import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import app as appmod  # noqa: E402
from backend import chat as chatmod  # noqa: E402
from backend import rainfall, wind  # noqa: E402


def _request(xff: str | None, client_ip: str = "10.0.0.1") -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    return Request({"type": "http", "headers": headers, "client": (client_ip, 1234)})


# ------------------------------------------------------- IP del cliente ------

def test_sin_proxy_confiado_se_ignora_x_forwarded_for(monkeypatch):
    monkeypatch.delenv("TRUST_PROXY", raising=False)
    assert appmod._client_ip(_request("1.2.3.4")) == "10.0.0.1"


def test_con_proxy_se_toma_la_entrada_que_anadio_el_proxy(monkeypatch):
    monkeypatch.setenv("TRUST_PROXY", "1")
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    # lo de la izquierda lo escribe el cliente; el ALB añade la IP real al final
    assert appmod._client_ip(_request("falsa-1, falsa-2, 203.0.113.9")) == "203.0.113.9"


def test_con_dos_proxies_se_cuenta_desde_la_derecha(monkeypatch):
    monkeypatch.setenv("TRUST_PROXY", "1")
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "2")
    assert appmod._client_ip(_request("falsa, 198.51.100.7, 10.1.1.1")) == "198.51.100.7"


def test_cabecera_mas_corta_que_los_saltos_usa_la_ip_del_socket(monkeypatch):
    monkeypatch.setenv("TRUST_PROXY", "1")
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "3")
    assert appmod._client_ip(_request("1.1.1.1")) == "10.0.0.1"
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "no-es-numero")
    assert appmod._client_ip(_request("falsa, 203.0.113.9")) == "203.0.113.9"


def test_rate_limit_no_se_evade_rotando_x_forwarded_for(monkeypatch):
    monkeypatch.setenv("TRUST_PROXY", "1")
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    appmod._hits.clear()
    client = TestClient(appmod.app)
    codes = [
        client.post("/api/chat", json={"message": ""},
                    headers={"X-Forwarded-For": f"10.9.0.{i}, 203.0.113.9"}).status_code
        for i in range(12)
    ]
    assert 429 in codes
    appmod._hits.clear()


# ------------------------------------------------------- poda de memoria -----

def test_hits_se_poda_aunque_las_ips_no_vuelvan(monkeypatch):
    appmod._hits.clear()
    t = [1000.0]
    monkeypatch.setattr(appmod.time, "monotonic", lambda: t[0])
    for i in range(appmod._MAX_TRACKED + 5):
        appmod._allowed(f"ip{i}", "default")
    assert len(appmod._hits) > appmod._MAX_TRACKED
    t[0] += 3600                                   # todas quedan fuera de la ventana
    appmod._allowed("nueva", "default")
    assert len(appmod._hits) < 10
    appmod._hits.clear()


def test_estado_del_chat_se_poda(monkeypatch):
    chatmod._rate_state.clear()
    chatmod._ip_locks.clear()
    t = [5000.0]
    monkeypatch.setattr(chatmod.time, "time", lambda: t[0])
    for i in range(chatmod._MAX_TRACKED + 5):
        chatmod._check_rate_limit(f"ip{i}")
        chatmod._lock_for(f"ip{i}")
    t[0] += 3600
    chatmod._check_rate_limit("nueva")
    chatmod._lock_for("otra")
    assert len(chatmod._rate_state) < 10
    assert len(chatmod._ip_locks) < 10
    chatmod._rate_state.clear()
    chatmod._ip_locks.clear()


def test_candado_en_uso_no_se_descarta():
    chatmod._ip_locks.clear()
    held = chatmod._lock_for("ocupada")
    held.acquire()
    try:
        for i in range(chatmod._MAX_TRACKED + 5):
            chatmod._lock_for(f"ip{i}")
        chatmod._lock_for("trigger")
        assert chatmod._lock_for("ocupada") is held
    finally:
        held.release()
        chatmod._ip_locks.clear()


# ------------------------------------------------------- CSP y HTML ----------

def _script_src_directive() -> str:
    m = re.search(r"script-src ([^;]+);", appmod.CSP)
    assert m
    return m.group(1)


def test_index_solo_carga_scripts_que_la_csp_permite():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    allowed = _script_src_directive().split()
    for src in re.findall(r'<script[^>]+src="([^"]+)"', html):
        if src.startswith("http"):
            origin = re.match(r"https?://[^/]+", src).group(0)
            assert origin in allowed, f"{src} no esta permitido por script-src"


def test_index_no_tiene_scripts_inline():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    for tag in re.findall(r"<script\b([^>]*)>(.*?)</script>", html, re.S):
        attrs, body = tag
        assert "src=" in attrs and not body.strip(), "script inline: la CSP lo bloquea"


def test_scripts_de_cdn_llevan_sri():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    for tag in re.findall(r"<(?:script|link)\b[^>]*(?:src|href)=\"https?://[^>]*>", html):
        assert 'integrity="sha384-' in tag and 'crossorigin="anonymous"' in tag, tag


# ------------------------------------------------------- piso de refresh -----

def _contar(monkeypatch, modulo, nombre):
    llamadas = []

    def falso():
        llamadas.append(1)
        return {"n": len(llamadas)}

    monkeypatch.setattr(modulo, nombre, falso)
    return llamadas


def test_refresh_no_salta_el_piso_en_lluvia(monkeypatch):
    import time
    llamadas = _contar(monkeypatch, rainfall, "_fetch_rainfall_24h")
    monkeypatch.setitem(rainfall._cache, "data", None)
    rainfall.fetch_rainfall_24h()                  # caché vacía: primera petición real
    assert len(llamadas) == 1
    rainfall._cache.update(data={"n": 1}, timestamp=time.time())
    for _ in range(5):
        rainfall.fetch_rainfall_24h(force=True)
    assert len(llamadas) == 1                      # 5 refresh seguidos no llegan a Open-Meteo
    rainfall._cache["timestamp"] -= 120            # pasado el piso, force sí refresca
    rainfall.fetch_rainfall_24h(force=True)
    assert len(llamadas) == 2
    rainfall._cache.update(data=None, timestamp=0)


def test_refresh_no_salta_el_piso_en_viento(monkeypatch):
    import time
    llamadas = _contar(monkeypatch, wind, "_fetch_wind_grid")
    monkeypatch.setitem(wind._cache, "data", {"n": 0})
    monkeypatch.setitem(wind._cache, "at", time.time())
    for _ in range(5):
        wind.fetch_wind_grid(force=True)
    assert llamadas == []
    wind._cache["at"] -= 120
    wind.fetch_wind_grid(force=True)
    assert len(llamadas) == 1
    wind._cache.update(data=None, at=0.0)
