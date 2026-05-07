import logging
from graph.state import InvestmentState
from clients.openai_client import chat

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 금융 뉴스 분석 전문가입니다.
오늘의 주요 뉴스 헤드라인을 분석하여 시장 영향 재료를 파악하세요.

분석 항목:
1. 시장에 영향을 미칠 핵심 뉴스 3-5건 선별
2. 각 뉴스의 시장 영향: 긍정·부정·중립 + 영향 섹터
3. 정책·경제 이슈 (금리, 환율, 규제 등)
4. 기업 이슈 (실적, M&A, 공시 등)
5. 뉴스 모멘텀이 있는 섹터·테마

출력: 핵심 뉴스 TOP3(제목+영향 한줄) / 주목 섹터·테마 / 전반 뉴스 센티먼트(긍정·부정·혼조)"""


def run(state: InvestmentState) -> InvestmentState:
    try:
        news = state.get("raw_news_data", {})
        lines = []
        for source, items in news.items():
            for item in items[:5]:
                if t := item.get("title"):
                    lines.append(f"[{source}] {t}")
        text = "\n".join(lines[:40]) if lines else "뉴스 없음"
        result = chat(_SYSTEM, f"오늘의 뉴스 헤드라인:\n{text}", max_tokens=2000)
        state["news_report"] = result
        logger.info("[뉴스팀] 완료")
    except Exception as e:
        logger.error("[뉴스팀] 실패: %s", e)
        state["news_report"] = "뉴스 수집 실패"
        state["errors"].append(f"news_team: {e}")
    return state
