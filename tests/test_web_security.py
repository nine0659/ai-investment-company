import base64
import importlib

from fastapi.testclient import TestClient


def _basic(password: str) -> dict[str, str]:
    token = base64.b64encode(f"user:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_status_and_health_remain_public(monkeypatch):
    monkeypatch.setenv("WEB_PASSWORD", "secret")
    import web.app as web_app
    importlib.reload(web_app)

    client = TestClient(web_app.app)

    assert client.get("/api/status").status_code == 200
    assert client.get("/health").status_code == 200


def test_private_api_requires_password(monkeypatch):
    monkeypatch.setenv("WEB_PASSWORD", "secret")
    import web.app as web_app
    importlib.reload(web_app)

    client = TestClient(web_app.app)

    assert client.get("/api/briefings").status_code == 401
    assert client.get("/api/briefings", headers=_basic("wrong")).status_code == 401
    assert client.get("/api/briefings", headers=_basic("secret")).status_code == 200


def test_private_api_closed_when_password_missing(monkeypatch):
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    import web.app as web_app
    importlib.reload(web_app)

    client = TestClient(web_app.app)

    assert client.get("/api/status").status_code == 200
    assert client.get("/api/briefings").status_code == 503


def test_health_accepts_head_for_uptime_monitors(monkeypatch):
    """슬립 방지 핑(UptimeRobot 기본 HEAD)이 405를 받으면 모니터가 오작동한다."""
    monkeypatch.setenv("WEB_PASSWORD", "secret")
    from web import app as web_app
    client = TestClient(web_app.app)
    assert client.head("/health").status_code == 200
