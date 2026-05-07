import logging
from graph.state import InvestmentState
from clients.openai_client import chat

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 섹터 순환매 및 테마 분석 전문가입니다.
여러 팀의 분석을 종합하여 오늘의 강세 섹터와 테마를 파악하세요.

분석 항목:
1. 미국 시장 기반 한국 수혜 섹터 (반도체·IT·자동차 등)
2. 뉴스 모멘텀 섹터·테마
3. 거래량·거래대금 기반 주도 섹터
4. 섹터별 강도 점수 (1~10점)
5. 순환매 흐름 파악

출력: TOP3 강세 섹터(점수 포함) / 약세 섹터 / 핵심 투자 테마 3개 / 순환매 신호"""

_SECTORS = ["반도체", "IT", "자동차", "2차전지", "바이오", "금융", "에너지", "건설", "소비재", "통신"]


def run(state: InvestmentState) -> InvestmentState:
    try:
        context = (
            f"[미국시장]\n{state.get('us_market_report', '')}\n\n"
            f"[한국현물]\n{state.get('korea_spot_report', '')}\n\n"
            f"[뉴스]\n{state.get('news_report', '')}"
        )
        result = chat(_SYSTEM, context, max_tokens=2000)
        state["sector_report"] = result
        state["sector_scores"] = [
            {"sector": s, "score": 60} for s in _SECTORS if s in result
        ]
        logger.info("[섹터팀] 완료")
    except Exception as e:
        logger.error("[섹터팀] 실패: %s", e)
        state["sector_report"] = "분석 실패"
        state["errors"].append(f"sector_team: {e}")
    return state
