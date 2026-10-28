"""LLM 응답에서 JSON을 꺼내는 공통 도구.

`re.search(r"\{.*\}", raw, DOTALL)` 같은 탐욕 정규식은 JSON 앞뒤에 설명·괄호가 붙거나
같은 JSON이 두 번 나오면 구간 전체가 깨져 조용히 None/빈 목록이 된다(2026-10-28 점검: 4개 파서가
같은 약점). 여기서는 각 여는 괄호에서 `raw_decode`로 첫 번째 완결된 값을 읽는다.
"""
from __future__ import annotations

import json
import re

_TRAILING_COMMA = re.compile(r",(\s*[\]}])")


def extract_json(raw: str, kind: type = dict):
    """raw 안에서 처음으로 올바르게 읽히는 JSON 값(kind: dict 또는 list)을 반환. 없으면 None.

    코드펜스·앞뒤 설명·뒤에 붙은 중복 JSON을 견디고, 끝 쉼표(,])는 한 번 정리해 다시 시도한다.
    """
    if not raw:
        return None
    opener = "{" if kind is dict else "["
    dec = json.JSONDecoder()
    for text in (raw, _TRAILING_COMMA.sub(r"\1", raw)):
        pos = text.find(opener)
        while pos != -1:
            try:
                val, _ = dec.raw_decode(text, pos)
                if isinstance(val, kind):
                    return val
            except ValueError:
                pass
            pos = text.find(opener, pos + 1)
    return None
