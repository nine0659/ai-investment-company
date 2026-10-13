import logging
from graph.state import InvestmentState
from clients.openai_client import chat
from services.review_service import get_last_close_report, save_review
from services.recommendation_service import get_recent_recommendations, get_performance_stats

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 투자 성과 복기 전문가입니다.
오늘 장 결과와 최근 30일 누적 패턴을 분석하여 내일 개선 방향을 도출하세요.

[절대 규칙]
- 아래 입력에 있는 숫자만 인용하세요. 점수(X/10)·승률·평균수익 등 새 통계를 만들거나 계산하지 마세요.
- 입력에 '표본 경고'가 있으면 패턴을 단정하지 말고 "가능성"으로만 쓰세요.
- 없는 데이터(RSI 조건별 성과 등)는 "데이터 없음"이라고 쓰고 넘어가세요.

[복기 항목]
1. 추천 종목 실적 — 입력에 있는 종목별 결과를 근거로 성공/실패 이유 (데이터 있을 때만)
2. 오늘 장전 예측 vs 실제 KOSPI — 입력의 예측·실제를 그대로 비교해 맞췄는지 명확히
3. 잘된 점 / 놓친 점: 어떤 신호가 유효했고 무엇을 놓쳤나 (구체적 사례, 입력 근거 있는 것만)
4. 반복 실수·편향 — 입력의 요일별·종목 쏠림 팩트에 근거한 것만

출력 형식:
- 오늘 성과: 입력의 건수·수익률을 그대로 옮겨 적기
- 예측 결과: 입력의 예측/실제 비교 한 줄 (점수 매기지 말 것)
- 잘된 점 2가지 (구체적 종목/섹터 근거)
- 개선점 2가지 (다음엔 어떤 신호를 추가해야 하는지)
- [30일 패턴]: 입력 팩트로 뒷받침되는 것만 1~2가지, 없으면 "표본 부족"
- 내일 집중 관찰: 딱 한 가지 (추상적 교훈 금지, 구체적 조건만)"""


def run(state: InvestmentState) -> InvestmentState:
    # state["errors"] 직접 mutate 금지 — 이유는 risk_management_team.py 참조.
    _new_errors: list[str] = []
    try:
        last    = get_last_close_report()
        mkt     = state.get("raw_market_data", {})
        kospi_d = mkt.get("kospi", {})
        date    = state.get("date", "")

        # 최근 추천 수익률 데이터 — 30일 패턴 분석용
        recent_recs = get_recent_recommendations(days=30)

        # 오늘 및 최근 3일 데이터 — 당일 복기 핵심
        today_recs = [r for r in recent_recs if r.get("return_pct") is not None][:10]
        if today_recs:
            rec_lines = []
            for r in today_recs:
                emoji = "✅" if r.get("result") == "성공" else ("❌" if r.get("result") == "실패" else "➖")
                ret = r.get("return_pct") or 0
                rec_lines.append(
                    f"{emoji} {r['date']} {r['name']}({r['code']}) "
                    f"진입 {r.get('entry_price',0):,}원 → 종가 {int(r.get('close_price') or 0):,}원 "
                    f"({ret:+.1f}%) [{r.get('result','?')}]"
                )
            rec_text = "최근 추천 성과 (수익률 확정분):\n" + "\n".join(rec_lines)
        else:
            rec_text = "최근 추천 종목 수익률: 데이터 없음"

        # 30일 누적 패턴 분석 데이터
        pattern_lines = []
        if len(recent_recs) >= 5:
            success_recs = [r for r in recent_recs if r.get("result") == "성공"]
            fail_recs    = [r for r in recent_recs if r.get("result") == "실패"]
            # 섹터 편향 파악 (종목코드 앞 3자리로 간략 분류)
            all_codes = [r.get("code", "") for r in recent_recs if r.get("code")]
            pattern_lines.append(
                f"[30일 누적] 총 {len(recent_recs)}건 | "
                f"성공 {len(success_recs)} | 실패 {len(fail_recs)} | "
                f"미확정 {len(recent_recs)-len(success_recs)-len(fail_recs)}"
            )
            if success_recs:
                avg_win = sum(r.get("return_pct",0) for r in success_recs) / len(success_recs)
                pattern_lines.append(f"  성공 평균수익률: {avg_win:+.2f}%")
            if fail_recs:
                avg_loss = sum(r.get("return_pct",0) for r in fail_recs) / len(fail_recs)
                pattern_lines.append(f"  실패 평균손실: {avg_loss:+.2f}%")
        pattern_text = "\n".join(pattern_lines) if pattern_lines else "패턴 분석 데이터 부족"

        # 30일 누적 통계 — 복기 AI가 패턴을 판단하는 기반 데이터
        stats = get_performance_stats(days=30)
        if stats["total"] >= 3:
            stats_text = (
                f"[최근 30일 추천 성과 통계]\n"
                f"총 {stats['total']}건 | 성공 {stats['win']}건 | 실패 {stats['loss']}건 | "
                f"승률 {stats['win_rate']}% | 평균수익률 {stats['avg_return']:+.2f}% | "
                f"최대손실 {stats['max_loss']:.2f}% | 손익비 {stats['profit_factor']:.2f}"
            )
        else:
            stats_text = "[최근 30일 추천 성과 통계] 데이터 부족 (3건 미만)"

        context_parts = [stats_text, rec_text, f"\n[30일 누적 패턴]\n{pattern_text}"]

        # 요일별 성과·종목 쏠림·표본 경고 — 코드가 계산 (LLM이 통계를 지어내지 못하게)
        try:
            from services.review_facts import build_review_facts
            context_parts.append("\n" + build_review_facts(recent_recs))
        except Exception as _rfe:
            logger.debug("[복기팀] 복기 팩트 계산 실패 (무시): %s", _rfe)

        # 오늘 장전 예측(구조화)과 최근 예측 적중률 — 점수 대신 사실로 비교
        try:
            from services.prediction_service import get_selfcheck_context, get_accuracy_summary
            _sc = get_selfcheck_context(date)
            if _sc:
                context_parts.append("\n" + _sc)
            _acc = get_accuracy_summary(days=20)
            if _acc:
                context_parts.append("\n" + _acc)
        except Exception as _pe:
            logger.debug("[복기팀] 예측 컨텍스트 주입 실패 (무시): %s", _pe)

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
        _new_errors.append(f"review_team: {e}")
    state["errors"] = _new_errors
    return state
