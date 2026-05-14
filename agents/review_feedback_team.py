import logging
from graph.state import InvestmentState
from clients.openai_client import chat
from services.review_service import get_last_close_report, save_review
from services.recommendation_service import get_recent_recommendations

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 투자 성과 복기 전문가입니다.
전일 분석 리포트, 실제 추천 종목 수익률, 오늘 장 결과를 비교하여 개선점을 도출하세요.

분석 항목:
1. 추천 종목 수익률 실적 — 성공/실패 종목별 이유 분석 (데이터가 있을 때만)
2. 전일 시장 방향 예측 vs 실제 KOSPI 등락 비교 (맞췄는지, 틀렸는지 명확히)
3. 잘된 점: 어떤 신호가 유효했는가 (구체적 사례)
4. 개선할 점: 어떤 신호를 놓쳤는가 (구체적 사례)
5. 내일을 위한 핵심 학습 포인트 1개 (추상적 교훈 금지, 구체적 조건만)

출력 형식:
- 추천 성과: N건 성공 / N건 실패 / 평균 수익률 X%
- 예측 정확도: X/10점 + 이유 한 줄
- 잘된 점 2가지 (구체적 종목/섹터 근거)
- 개선점 2가지 (다음엔 어떤 신호를 추가해야 하는지)
- 내일 집중 관찰: 딱 한 가지"""


def run(state: InvestmentState) -> InvestmentState:
    try:
        last    = get_last_close_report()
        mkt     = state.get("raw_market_data", {})
        kospi_d = mkt.get("kospi", {})
        date    = state.get("date", "")

        # 최근 추천 수익률 데이터 (오늘 포함 3일)
        recent_recs = get_recent_recommendations(days=3)
        if recent_recs:
            rec_lines = []
            for r in recent_recs:
                emoji = "✅" if r.get("result") == "성공" else ("❌" if r.get("result") == "실패" else "➖")
                ret = r.get("return_pct") or 0
                rec_lines.append(
                    f"{emoji} {r['date']} {r['name']}({r['code']}) "
                    f"진입 {r.get('entry_price',0):,}원 → 종가 {int(r.get('close_price') or 0):,}원 "
                    f"({ret:+.1f}%) [{r.get('result','?')}]"
                )
            rec_text = "최근 추천 종목 수익률:\n" + "\n".join(rec_lines)
        else:
            rec_text = "최근 추천 종목 수익률: 데이터 없음"

        context_parts = [rec_text]

        if last:
            context_parts.append(
                f"\n전일 리포트 ({last.get('date', '')}):\n"
                f"{last.get('ceo_report', '')[:800]}"
            )

        context_parts.append(
            f"\n오늘 KOSPI: {kospi_d.get('close', 'N/A')} ({kospi_d.get('change_pct', 0):+.2f}%)\n"
            f"오늘 뉴스 요약: {state.get('news_report', '')[:400]}"
        )

        context = "\n".join(context_parts)
        result = chat(_SYSTEM, context, max_tokens=1500)
        state["review_report"] = result
        save_review(date, result)
        logger.info("[복기팀] 완료")
    except Exception as e:
        logger.error("[복기팀] 실패: %s", e)
        state["review_report"] = "복기 생성 실패"
        state["errors"].append(f"review_team: {e}")
    return state
