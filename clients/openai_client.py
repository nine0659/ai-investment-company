import logging
from collections.abc import Generator
from openai import OpenAI
from config.settings import (
    OPENAI_API_KEY, OPENAI_MODEL, OPENAI_MODEL_CEO,
    OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_MODEL_CEO,
)

logger = logging.getLogger(__name__)

_client: OpenAI | None = None
_fallback_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


def get_fallback_client() -> OpenAI | None:
    """OPENROUTER_API_KEY 미설정 시 None — 폴백 없이 기존 동작 유지."""
    global _fallback_client
    if not OPENROUTER_API_KEY:
        return None
    if _fallback_client is None:
        _fallback_client = OpenAI(
            api_key=OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1",
        )
    return _fallback_client


def chat_ceo(system: str, user: str, max_tokens: int = 2000) -> str:
    """CEO 전용 — OPENAI_MODEL_CEO 사용 (기본: gpt-4o)"""
    return chat(
        system, user,
        model=OPENAI_MODEL_CEO,
        fallback_model=OPENROUTER_MODEL_CEO,
        max_tokens=max_tokens,
    )


def chat(
    system: str,
    user: str,
    model: str | None = None,
    fallback_model: str | None = None,
    max_tokens: int = 2000,
) -> str:
    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": user},
    ]
    try:
        resp = get_client().chat.completions.create(
            model=model or OPENAI_MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.3,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        fallback = get_fallback_client()
        if fallback is None:
            raise
        logger.warning("[OpenAI] 호출 실패, OpenRouter로 폴백: %s", e)
        resp = fallback.chat.completions.create(
            model=fallback_model or OPENROUTER_MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.3,
        )
        return resp.choices[0].message.content.strip()


def chat_stream(
    system: str,
    user: str,
    history: list[dict] | None = None,
    model: str | None = None,
    max_tokens: int = 3000,
) -> Generator[str, None, None]:
    """텍스트 청크를 yield 하는 스트리밍 버전.
    history: [{"role": "user"|"assistant", "content": "..."}, ...]
    """
    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user})

    try:
        stream = get_client().chat.completions.create(
            model=model or OPENAI_MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.4,
            stream=True,
        )
    except Exception as e:
        fallback = get_fallback_client()
        if fallback is None:
            raise
        logger.warning("[OpenAI] 스트리밍 호출 실패, OpenRouter로 폴백: %s", e)
        stream = fallback.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.4,
            stream=True,
        )

    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta
