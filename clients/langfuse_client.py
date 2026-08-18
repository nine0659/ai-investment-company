"""clients/langfuse_client.py — Langfuse Cloud 관측성 콜백 (선택사항).

LANGFUSE_PUBLIC_KEY/SECRET_KEY 미설정 시 콜백 없이 기존과 완전히 동일하게
동작한다 — OpenRouter 폴백(clients/openai_client.py)과 같은 "없으면 조용히
빠짐" 패턴. Langfuse SDK 자체가 OpenTelemetry 기반 백그라운드 배치 전송이라,
호출 시점에 네트워크 왕복이 없다 — Langfuse 장애나 무료 티어 한도 초과가
브리핑 파이프라인을 막을 수 없다(구성 실패는 로그만 남기고 빈 리스트 반환).

graph.invoke(initial, config={"callbacks": get_langfuse_callbacks()})로 넘기면
LangGraph 노드 실행 경계(어느 브랜치가 얼마나 걸렸는지)가 하나의 트레이스로
묶인다. clients/openai_client.py가 별도로 langfuse.openai.OpenAI 드롭인을
쓰면 그 안의 실제 LLM 호출(프롬프트·토큰·비용)도 같은 트레이스에 중첩된다
(둘 다 OpenTelemetry 컨텍스트를 공유하므로).
"""
import logging

from config.settings import LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY

logger = logging.getLogger(__name__)

_warned = False


def langfuse_configured() -> bool:
    return bool(LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY)


def get_langfuse_callbacks() -> list:
    """graph.invoke(config={"callbacks": [...]})에 넣을 콜백 리스트. 미설정/실패 시 빈 리스트."""
    global _warned
    if not langfuse_configured():
        return []
    try:
        from langfuse.langchain import CallbackHandler
        return [CallbackHandler()]
    except Exception as e:
        if not _warned:
            logger.warning("[Langfuse] 콜백 생성 실패 (관측성 없이 계속 진행): %s", e)
            _warned = True
        return []
