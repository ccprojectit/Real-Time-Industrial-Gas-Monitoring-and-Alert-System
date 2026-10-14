from pathlib import Path
import sys

import pytest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.middleware import Middleware

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import main


PRODUCTION_ORIGIN = "https://industrial-gas-monitoring-dashboard.vercel.app"


@pytest.fixture(autouse=True)
def no_external_services(monkeypatch):
    async def no_database_connect():
        return None

    async def no_database_close():
        return None

    monkeypatch.setattr(main, "connect", no_database_connect)
    monkeypatch.setattr(main, "close", no_database_close)


def production_cors(monkeypatch):
    """Build the real app middleware stack with the deployed exact origin."""
    monkeypatch.setattr(main.settings, "cors_origins", PRODUCTION_ORIGIN)
    middlewares = []
    for middleware in main.app.user_middleware:
        if middleware.cls is CORSMiddleware:
            options = dict(middleware.kwargs)
            options["allow_origins"] = [PRODUCTION_ORIGIN]
            middlewares.append(Middleware(CORSMiddleware, **options))
        else:
            middlewares.append(middleware)
    monkeypatch.setattr(main.app, "user_middleware", middlewares)
    monkeypatch.setattr(main.app, "middleware_stack", None)


def test_production_origin_preflight_allows_api_methods_and_auth_headers(monkeypatch):
    production_cors(monkeypatch)
    with TestClient(main.app, client=("127.0.0.1", 1234)) as client:
        response = client.options(
            "/api/state",
            headers={
                "Origin": PRODUCTION_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == PRODUCTION_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "authorization" in response.headers["access-control-allow-headers"].lower()
    assert "content-type" in response.headers["access-control-allow-headers"].lower()


def test_preflight_rejects_an_unlisted_origin(monkeypatch):
    production_cors(monkeypatch)
    with TestClient(main.app, client=("127.0.0.1", 1234)) as client:
        response = client.options(
            "/api/state",
            headers={
                "Origin": "https://untrusted.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "content-type",
            },
        )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_preflight_does_not_bypass_operator_authentication(monkeypatch):
    production_cors(monkeypatch)
    monkeypatch.setattr(main.settings, "app_env", "production")
    monkeypatch.setattr(main.settings, "auth_required", True)
    monkeypatch.setattr(main.settings, "database_url", "postgresql://test.invalid/database")
    monkeypatch.setattr(main.settings, "session_secret", "cors-test-session-secret-with-more-than-32-characters")
    monkeypatch.setattr(main.settings, "admin_username", "production-operator")
    monkeypatch.setattr(main.settings, "admin_password", "cors-test-password-that-is-long-enough")

    async def idle_simulator():
        import asyncio
        await asyncio.sleep(3600)

    async def no_broadcast():
        return None

    monkeypatch.setattr(main, "simulator_loop", idle_simulator)
    monkeypatch.setattr(main, "broadcast", no_broadcast)
    with TestClient(main.app, base_url="https://testserver", client=("127.0.0.1", 1234)) as client:
        headers = {"Origin": PRODUCTION_ORIGIN}
        preflight = client.options(
            "/api/simulation/start",
            headers={**headers, "Access-Control-Request-Method": "POST",
                     "Access-Control-Request-Headers": "content-type"},
        )
        assert preflight.status_code == 200

        denied = client.post("/api/simulation/start", headers=headers, json={})
        assert denied.status_code == 401
        assert denied.headers["access-control-allow-origin"] == PRODUCTION_ORIGIN

        login = client.post(
            "/api/auth/login",
            headers=headers,
            json={"username": "production-operator", "password": "cors-test-password-that-is-long-enough"},
        )
        assert login.status_code == 200
        cookie = login.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=none" in cookie
        session = client.get("/api/auth/session", headers=headers)
        assert session.status_code == 200
        assert session.json()["authenticated"] is True
        assert session.json()["auth_required"] is True

        started = client.post("/api/simulation/start", headers=headers, json={})
        assert started.status_code == 200
        assert started.headers["access-control-allow-origin"] == PRODUCTION_ORIGIN
        stopped = client.post("/api/simulation/stop", headers=headers, json={})
        assert stopped.status_code == 200
        logout = client.post("/api/auth/logout", headers=headers)
        assert logout.status_code == 200
        logout_cookie = logout.headers["set-cookie"].lower()
        assert "secure" in logout_cookie and "httponly" in logout_cookie and "samesite=none" in logout_cookie
        assert client.get("/api/auth/session", headers=headers).json()["authenticated"] is False
