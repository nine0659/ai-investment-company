import logging
from graph.state import InvestmentState
from clients.openai_client import chat
from services.review_service import get_last_close_report, save_review

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 투자 성과 복기 전문가입니다.
전일 분석 리포트와 오늘 장 결과를 비교하여 개선점을 도출하세요.

분석 항목:
1. 전일 예측 vs 실제 결과 비교
2. 잘된 점 (맞춘 예측, 효과적인 분석)
3. 개선할 점 (틀린 예측, 놓친 신호)
4. 내일을 위한 학습 포인트
5. 분석 프레임워크 개선 제안

출력: 예측 정확도(점수/10점) / 잘된 점 2-3가지 / 개선점 2-3가지 / 내일 주목할 것"""


def run(state: InvestmentState) -> InvestmentState:
    try:
        last = get_last_close_report()
        mkt  = state.get("raw_market_data", {})
        kospi_d = mkt.get("kospi", {})

        if last:
            context = (
                f"전일 리포트 ({last.get('date', '')}):\n"
                f"{last.get('ceo_report', '')[:1000]}\n\n"
                f"오늘 KOSPI: {kospi_d.get('close', 'N/A')} ({kospi_d.get('change_pct', 0):+.2f}%)\n"
                f"오늘 뉴스: {state.get('news_report', '')[:500]}"
            )
        else:
            context = "전일 리포트 없음 (첫 실행)"

        result = chat(_SYSTEM, context, max_tokens=2000)
        state["review_report"] = result
        save_review(state.get("date", ""), result)
        logger.info("[복기팀] 완료")
    except Exception as e:
        logger.error("[복기팀] 실패: %s", e)
        state["review_report"] = "복기 생성 실패"
        state["errors"].append(f"review_team: {e}")
    return state
