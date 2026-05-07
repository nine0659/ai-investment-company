import logging
from graph.state import InvestmentState
from clients.openai_client import chat
from services.scoring_service import score_stock

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 수급 분석 전문가입니다.
후보 종목들의 수급 데이터를 분석하여 오늘 매매 집중 가능성이 높은 종목을 점수화하세요.

분석 항목:
1. 거래량 급증 + 주가 상승 종목 (수급 유입 신호)
2. 기관·외국인 순매수 추정 종목
3. 상승 모멘텀 지속 종목
4. 섹터 강도와 일치하는 종목
5. 위험 대비 수익 비율이 좋은 종목

출력: 수급 집중 종목 TOP5(이유 포함) / 각 매수 강도(강·중·약) / 전체 수급 판단(매수우위·매도우위·중립)"""


def run(state: InvestmentState) -> InvestmentState:
    try:
        candidates = state.get("candidates", [])
        sector_scores = state.get("sector_scores", [])

        for c in candidates:
            c["score"] = score_stock(c, sector_scores)
        candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
        state["candidates"] = candidates

        top10 = candidates[:10]
        stock_text = "\n".join(
            f"{c.get('name', c.get('code', ''))}: 등락률 {c.get('change_pct', 0)}%, 점수 {c.get('score', 0)}"
            for c in top10
        ) or "후보 없음"

        context = f"후보 종목:\n{stock_text}\n\n섹터 분석:\n{state.get('sector_report', '')}"
        result = chat(_SYSTEM, context, max_tokens=2000)
        state["money_flow_report"] = result
        logger.info("[수급팀] 완료")
    except Exception as e:
        logger.error("[수급팀] 실패: %s", e)
        state["money_flow_report"] = "분석 실패"
        state["errors"].append(f"money_flow_team: {e}")
    return state
