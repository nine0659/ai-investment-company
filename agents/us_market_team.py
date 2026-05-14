import logging
from graph.state import InvestmentState
from clients.openai_client import chat
from clients.us_stock_client import format_us_impact_for_prompt

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 미국 증시 → 한국 증시 연동 분석 전문가입니다.

핵심 원칙: 전일 미국 증시에서 일어난 일은 시차를 두고 한국 증시에서 반복됩니다.
미국 강세 섹터 = 오늘 한국 강세 섹터의 가장 강력한 선행 지표입니다.

분석 항목:
1. 전일 미국 3대 지수(S&P500·NASDAQ·DOW) 성과 → 오늘 KOSPI·KOSDAQ 방향성 판단
2. SOX 반도체 지수 → SK하이닉스·삼성전자·한미반도체 등 오늘 방향 직결
3. 전일 미국 상위 급등 종목 TOP5 — 한국 공급망·경쟁사 연관 종목 명시
4. 미국 섹터 강세 순위 → 오늘 한국 매수 우선순위 도출
5. 미국 실적발표·뉴스 이벤트 → 오늘 한국 시장 선반영 여부

출력 형식:
- [미국 증시 → 오늘 한국 방향] 한 문장 결론 + 반드시 확률 포함
  예: "NASDAQ +1.8%, SOX +2.3% → 오늘 반도체 갭업 확률 75%, 갭다운 확률 25%"
  예: "S&P500 -0.8%, 달러강세 → KOSPI 상승확률 30%, 하락확률 70%"
- 전일 미국 급등 종목 TOP5 + 한국 수혜 종목 (공급망 근거 명시, 수혜 강도: 강/중/약)
- [오늘 코스피·코스닥 이슈 종목] 섹터별 종목명(코드) — 연동 이유 + 모멘텀 지속 확률
- [오늘 피해야 할 섹터] 미국 약세 섹터 연동 + 하락 확률"""

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
