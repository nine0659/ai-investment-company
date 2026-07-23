"""OpenAI 실패 시 OpenRouter 폴백 회귀 테스트.

OpenAI 크레딧 소진/장애 시 브리핑이 조용히 죽는 대신 OpenRouter로 넘어가야 한다.
OPENROUTER_API_KEY 미설정 시에는 기존과 동일하게 예외가 그대로 전파돼야 한다
(폴백을 켠 적 없는 사용자의 동작을 바꾸면 안 됨).

client 모듈의 OPENROUTER_API_KEY 속성을 직접 monkeypatch한다 — 실제 .env에
OPENROUTER_API_KEY가 설정된 개발 환경에서도(운영 배포 대상 머신 포함) 테스트가
그 값에 좌우되지 않고 항상 격리되게 하기 위함.
"""
import clients.openai_client as client_module
import pytest


class _FakeCompletions:
    def __init__(self, fn):
        self._fn = fn

    def create(self, **kwargs):
        return self._fn(**kwargs)


class _FakeChat:
    def __init__(self, fn):
        self.completions = _FakeCompletions(fn)


class _FakeClient:
    def __init__(self, fn):
        self.chat = _FakeChat(fn)


def _resp(text):
    class _Msg:
        content = text
    class _Choice:
        message = _Msg()
    class _Resp:
        choices = [_Choice()]
    return _Resp()


def test_chat_no_fallback_configured_reraises(monkeypatch):
    """OPENROUTER_API_KEY 미설정 — 기존 동작 그대로 예외 전파."""
    monkeypatch.setattr(client_module, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(client_module, "_fallback_client", None)

    def _boom(**kwargs):
        raise RuntimeError("insufficient_quota")

    monkeypatch.setattr(client_module, "get_client", lambda: _FakeClient(_boom))

    with pytest.raises(RuntimeError, match="insufficient_quota"):
        client_module.chat("sys", "user")


def test_chat_falls_back_to_openrouter_on_failure(monkeypatch):
    """OpenAI 실패 + OPENROUTER_API_KEY 설정 — OpenRouter로 재시도해 정상 응답."""
    calls = {}

    def _boom(**kwargs):
        raise RuntimeError("insufficient_quota")

    def _fallback_ok(**kwargs):
        calls["model"] = kwargs.get("model")
        return _resp("폴백 응답")

    monkeypatch.setattr(client_module, "get_client", lambda: _FakeClient(_boom))
    monkeypatch.setattr(client_module, "get_fallback_client", lambda: _FakeClient(_fallback_ok))

    result = client_module.chat("sys", "user", fallback_model="openai/gpt-4.1-mini")

    assert result == "폴백 응답"
    assert calls["model"] == "openai/gpt-4.1-mini"


def test_chat_primary_success_never_touches_fallback(monkeypatch):
    """OpenAI가 정상 응답하면 폴백 클라이언트는 아예 생성/호출되지 않아야 한다."""
    def _ok(**kwargs):
        return _resp("정상 응답")

    def _should_not_be_called():
        raise AssertionError("OpenAI 성공 시 폴백 클라이언트를 만들면 안 됨")

    monkeypatch.setattr(client_module, "get_client", lambda: _FakeClient(_ok))
    monkeypatch.setattr(client_module, "get_fallback_client", _should_not_be_called)

    assert client_module.chat("sys", "user") == "정상 응답"


def test_both_openai_and_fallback_fail_raises_fallback_error(monkeypatch):
    """양쪽 다 실패하면 예외가 상위(스케줄러의 텔레그램 경보 경로)로 전파돼야 한다."""
    def _boom_primary(**kwargs):
        raise RuntimeError("openai down")

    def _boom_fallback(**kwargs):
        raise RuntimeError("openrouter down")

    monkeypatch.setattr(client_module, "get_client", lambda: _FakeClient(_boom_primary))
    monkeypatch.setattr(client_module, "get_fallback_client", lambda: _FakeClient(_boom_fallback))

    with pytest.raises(RuntimeError, match="openrouter down"):
        client_module.chat("sys", "user")


def test_get_fallback_client_returns_none_without_key(monkeypatch):
    monkeypatch.setattr(client_module, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(client_module, "_fallback_client", None)
    assert client_module.get_fallback_client() is None


def test_get_fallback_client_builds_client_with_key(monkeypatch):
    monkeypatch.setattr(client_module, "OPENROUTER_API_KEY", "sk-or-test-key")
    monkeypatch.setattr(client_module, "_fallback_client", None)
    fb = client_module.get_fallback_client()
    assert fb is not None
    assert str(fb.base_url).rstrip("/") == "https://openrouter.ai/api/v1"
