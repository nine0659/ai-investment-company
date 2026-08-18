"""Langfuse 관측성 — 미설정 시 완전 무동작(no-op) 회귀 테스트.

2026-08-18: Langfuse Cloud 무료 티어 도입(B그룹 재분류). 아직 키를 발급받지
않은 상태이므로, LANGFUSE_PUBLIC_KEY/SECRET_KEY 미설정 상태에서 파이프라인이
기존과 완전히 동일하게 동작하는 게 가장 중요한 계약이다 — 이 테스트가 그걸 고정한다.
"""
import clients.langfuse_client as langfuse_client_module
import clients.openai_client as openai_client_module


def test_get_langfuse_callbacks_empty_without_keys(monkeypatch):
    monkeypatch.setattr(langfuse_client_module, "LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setattr(langfuse_client_module, "LANGFUSE_SECRET_KEY", "")
    assert langfuse_client_module.get_langfuse_callbacks() == []


def test_get_langfuse_callbacks_empty_with_only_public_key(monkeypatch):
    """둘 다 있어야 한다 — 한쪽만 설정된 반쪽 상태에서 조용히 켜지면 안 됨."""
    monkeypatch.setattr(langfuse_client_module, "LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setattr(langfuse_client_module, "LANGFUSE_SECRET_KEY", "")
    assert langfuse_client_module.get_langfuse_callbacks() == []


def test_get_langfuse_callbacks_returns_handler_when_configured(monkeypatch):
    monkeypatch.setattr(langfuse_client_module, "LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setattr(langfuse_client_module, "LANGFUSE_SECRET_KEY", "sk-lf-test")
    callbacks = langfuse_client_module.get_langfuse_callbacks()
    assert len(callbacks) == 1


def test_get_langfuse_callbacks_swallows_construction_errors(monkeypatch):
    """SDK가 예외를 던져도(버전 불일치 등) 빈 리스트로 조용히 넘어가야 한다 — 무사고 원칙."""
    monkeypatch.setattr(langfuse_client_module, "LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setattr(langfuse_client_module, "LANGFUSE_SECRET_KEY", "sk-lf-test")

    import langfuse.langchain

    def _boom(*a, **k):
        raise RuntimeError("SDK 버전 불일치 시뮬레이션")

    monkeypatch.setattr(langfuse.langchain, "CallbackHandler", _boom)
    assert langfuse_client_module.get_langfuse_callbacks() == []


def test_openai_client_uses_plain_openai_without_langfuse_keys():
    """Langfuse 미설정 상태에서는 openai.OpenAI 그대로 — 드롭인으로 안 바뀜."""
    import openai
    assert openai_client_module.OpenAI is openai.OpenAI
