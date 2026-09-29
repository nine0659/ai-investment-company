"""주간 추천 프롬프트(midterm_agent) 출력 형식 ↔ recs_from_weekly_picks 정규식 계약 테스트.

프롬프트의 "1. 종목명 (코드) — 현재가 X원 → 목표가 Y원 (…)" 예시 줄을 소스에서 직접
추출해 값을 채운 뒤 파서에 넣는다. 프롬프트 문구를 고쳐서 정규식이 더 이상 못 읽으면
추천 파싱이 조용히 0건이 되고 학습 루프(추천→추적)가 멈춘다 — 여기서 먼저 깨진다.
"""
import pathlib
import re

from services.recommendation_service import recs_from_weekly_picks

_SRC = pathlib.Path(__file__).resolve().parent.parent / "agents" / "midterm_agent.py"


def _prompt_format_line() -> str:
    for line in _SRC.read_text(encoding="utf-8").splitlines():
        if re.match(r"\s*1\.\s*종목명\s*\(코드\)", line):
            return line.strip()
    raise AssertionError("midterm 프롬프트에서 '1. 종목명 (코드) — …' 형식 줄을 찾지 못함 — 파서와 함께 확인 필요")


def test_prompt_format_line_is_parseable_by_pick_regex():
    line = _prompt_format_line()
    filled = (line.replace("종목명", "삼성전자").replace("(코드)", "(005930)")
                  .replace("X원", "309,500원").replace("Y원", "370,000원")
                  .replace("+Z%", "+19.5%").replace("-N%", "-10%"))
    recs = recs_from_weekly_picks(filled, {"005930": 309500})
    assert len(recs) == 1, f"프롬프트 예시 형식을 파서가 못 읽음: {filled!r}"
    assert recs[0]["code"] == "005930"
    assert recs[0]["entry_price"] == 309500      # 항상 실데이터 가격
    assert recs[0]["target_price"] == 370000
