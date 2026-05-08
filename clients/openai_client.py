from openai import OpenAI
from config.settings import OPENAI_API_KEY, OPENAI_MODEL, OPENAI_MODEL_CEO

_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


def chat_ceo(system: str, user: str, max_tokens: int = 2000) -> str:
    """CEO 전용 — OPENAI_MODEL_CEO 사용 (기본: gpt-4o)"""
    return chat(system, user, model=OPENAI_MODEL_CEO, max_tokens=max_tokens)


def chat(system: str, user: str, model: str | None = None, max_tokens: int = 2000) -> str:
    resp = get_client().chat.completions.create(
        model=model or OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        max_tokens=max_tokens,
        temperature=0.3,
    )
    return resp.choices[0].message.content.strip()
