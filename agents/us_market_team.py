import logging
from graph.state import InvestmentState
from clients.openai_client import chat
from clients.us_stock_client import format_us_impact_for_prompt

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 미국 주식시장 분석 및 한국 시장 영향 전문가입니다.
미국 증시 데이터와 주목 종목을 바탕으로 오늘 한국 시장 영향을 분석하세요.

분석 항목:
1. 미국 3대 지수(S&P500·NASDAQ·DOW) 전일 성과 및 방향성
2. 반도체 지수(SOX) 동향
3. 미국 시장 주목 종목 TOP5 — 거래량 급증·등락 이유
4. 오늘 KOSPI에서 이슈될 한국 연관 종목 (미국 종목과의 공급망·경쟁 관계 명시)
5. 오늘의 핵심 시그널 한 줄

출력 형식:
- 미국 지수 요약 (긍정/부정/혼조)
- 주목 미국 종목 TOP5 + 한국 연관 종목
- [오늘 코스피 이슈 종목] 섹션: 종목명(코드) — 이유
- 핵심 시그널"""

_INDEX_LABELS = {
    "sp500": "S&P500", "nasdaq": "NASDAQ", "dow": "DOW",
    "sox": "SOX반도체지수", "nvda": "NVIDIA", "tsmc": "TSMC",
}


def run(state: InvestmentState) -> InvestmentState:
    try:
        data = state.get("raw_market_data", {})
        index_lines = [
            f"{_INDEX_LABELS[k]}: {d['close']} ({d['change_pct']:+.2f}%)"
            for k in _INDEX_LABELS if (d := data.get(k))
        ]

        us_hot = state.get("us_hot_stocks", [])
        us_impact_text = format_us_impact_for_prompt(us_hot)

        context = (
            "=== 미국 지수 ===\n"
            + ("\n".join(index_lines) or "데이터 없음")
            + "\n\n=== 미국 거래량·등락 상위 종목 → 한국 연관 종목 ===\n"
            + us_impact_text
        )

        result = chat(_SYSTEM, context, max_tokens=1500)
        state["us_market_report"] = result
        logger.info("[미국팀] 완료")
    except Exception as e:
        logger.error("[미국팀] 실패: %s", e)
        state["us_market_report"] = "데이터 수집 실패"
        state["errors"].append(f"us_team: {e}")
    return state
