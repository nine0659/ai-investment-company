import logging
from graph.state import InvestmentState
from clients.openai_client import chat

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 금융 뉴스 분석 전문가입니다.

핵심 원칙: 미국에서 발생한 경제·정책·기업 이슈는 한국 시장에 직접 파급됩니다.
미국 뉴스를 1순위, 글로벌 뉴스 2순위, 한국 뉴스 3순위로 분석하세요.

분석 항목:
1. [미국 핵심 뉴스] Fed 발언·금리·고용·CPI·실적 등 매크로 이슈 → 한국 영향 명시
2. [글로벌 이슈] 중국 경제·유럽 금리·지정학 → 수출주·원자재 연동 효과
3. [한국 뉴스] 국내 정책·실적·수급 이슈
4. 각 뉴스의 한국 수혜 섹터·종목 (구체적 종목명 가능 시 명시)
5. 오늘 시장 전체 뉴스 센티먼트 판단

출력:
- [미국발 오늘 핵심 재료] TOP3 (제목 + 한국 영향 한줄)
- [글로벌·국내 보조 재료] TOP2
- [뉴스 기반 오늘 주목 섹터] (긍정/부정 구분)
- 전체 뉴스 센티먼트: 긍정·부정·혼조"""


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
