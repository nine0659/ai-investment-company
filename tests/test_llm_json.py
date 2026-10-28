"""LLM JSON 파서 공통 견고성 — 2026-10-28 점검: 탐욕 정규식(\{.*\})은 JSON 뒤에 설명·괄호가 붙거나
같은 JSON이 두 번 나오면 조용히 실패했다(예측·교훈·리서치·업종 전망 4개 파서가 같은 약점)."""
import pytest

from services.forecast_loop import parse_forecast
from services.learning_loop import parse_reflection
from services.llm_json import extract_json
from services.report_analyzer import parse_json_object
from services import sector_outlook as so

FC = '{"방향":"상승","확률":62,"하단":-0.5,"상단":1.8,"근거":"외국인 순매수","틀리는조건":"환율 1400 돌파"}'
REFL = '[{"분석":"a","사고":"b","판단":"c","교훈":"변동성 큰 날 중립 금지","의견":"d"}]'
SEC = '{"판단":[{"업종번호":"278","전망":"우호","확률":60,"근거":"PER 할인 12%","틀리는조건":"x"}]}'
ROWS = [{"ind_no": "278", "name": "반도체"}]

WRAPS = [
    lambda s: s,
    lambda s: "```json\n" + s + "\n```",
    lambda s: "분석 결과입니다.\n" + s + "\n\n참고로 [중요]한 점은 없습니다.",
    lambda s: s + "\n(주: {표본 부족} 가능)",
    lambda s: s + "\n\n수정본:\n" + s,
]


@pytest.mark.parametrize("wrap", WRAPS)
def test_all_four_parsers_survive_wrappers(wrap):
    assert parse_forecast(wrap(FC))["direction"] == "상승"
    assert parse_reflection(wrap(REFL))[0]["lesson"].startswith("변동성")
    assert parse_json_object(wrap(FC))["방향"] == "상승"
    assert len(so.parse_views(wrap(SEC), ROWS, "PER 할인 12%")) == 1


def test_trailing_comma_tolerated():
    assert extract_json('{"a": [1, 2,], "b": 1,}', dict) == {"a": [1, 2], "b": 1}


def test_garbage_returns_none_not_default():
    assert extract_json("JSON이 아닙니다", dict) is None
    assert extract_json("", list) is None
    assert parse_forecast("{깨짐") is None
    assert parse_reflection("[깨짐") == []

