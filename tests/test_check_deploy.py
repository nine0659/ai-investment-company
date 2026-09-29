"""2026-09-21 사고 회귀: Render 배포 실패 시 옛 컨테이너가 계속 돌아도 경보가 없었다."""
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "check_deploy", pathlib.Path(__file__).resolve().parent.parent / "scripts" / "check_deploy.py"
)
check_deploy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_deploy)

SHA = "a" * 40


def test_matching_commit_is_ok():
    ok, _ = check_deploy.evaluate({"ok": True, "commit": SHA}, SHA)
    assert ok


def test_old_container_without_commit_field_is_flagged():
    ok, reason = check_deploy.evaluate({"ok": True, "market": "closed"}, SHA)
    assert not ok and "commit 필드" in reason


def test_stale_commit_is_flagged():
    ok, reason = check_deploy.evaluate({"ok": True, "commit": "b" * 40}, SHA)
    assert not ok and "다릅니다" in reason


def test_no_response_is_flagged():
    ok, _ = check_deploy.evaluate(None, SHA)
    assert not ok


def test_status_endpoint_exposes_commit(monkeypatch):
    import importlib
    from fastapi.testclient import TestClient

    monkeypatch.setenv("RENDER_GIT_COMMIT", SHA)
    import web.app as web_app
    importlib.reload(web_app)
    assert TestClient(web_app.app).get("/api/status").json()["commit"] == SHA
