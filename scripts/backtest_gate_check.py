"""scripts/backtest_gate_check.py

추천/목표가/손절가 계산 로직(services/recommendation_service.py,
agents/midterm_stock_agent.py, agents/us_market 관련 에이전트 등)을 바꾸기
전에, 과거 추천이 실제로 어떻게 됐는지 먼저 보고 바꾸자는 취지의 수동 게이트.

CI에 안 물렸다 — stock_recommendations이 아직 소량(2026-08 기준 3건)이라
자동 pass/fail 임계값을 걸면 표본 부족을 "실패"로 오판하거나, 반대로 뭘
걸러도 의미가 없다. 대신 로직을 바꾸기 전 사람이 한 번 보고 판단하는
체크리스트로 쓴다 — services/backtest_service.py(기존 웹 대시보드
/api/backtest, /api/performance가 쓰던 계산)를 그대로 재사용한다.

사용법:
    python scripts/backtest_gate_check.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_MIN_SAMPLE_FOR_SIGNAL = 5  # 이 미만이면 승률·평균수익 숫자를 참고만 하고 판단 근거로 쓰지 말 것


def main() -> int:
    from services.backtest_service import get_recommendation_backtest, get_portfolio_performance

    rec = get_recommendation_backtest(days=20)
    r_stats = rec.get("stats", {})
    print("=== AI 추천 종목 백테스트 (20영업일 후 수익률) ===")
    if rec.get("error"):
        print(f"[조회 실패] {rec['error']}")
    else:
        print(f"전체 {r_stats.get('total', 0)}건 | 유효 {r_stats.get('valid', 0)}건 | "
              f"승 {r_stats.get('wins', 0)} / 패 {r_stats.get('losses', 0)}")
        print(f"승률 {r_stats.get('win_rate', 0)}% | 평균수익 {r_stats.get('avg_return', 0)}% "
              f"(평균승 {r_stats.get('avg_win', 0)}% / 평균패 {r_stats.get('avg_loss', 0)}%)")
        for r in rec.get("items", []):
            print(f"  {r['date']} {r['name']}({r['code']}): "
                  f"{r['return_pct']}% (목표 {r['target_ret']}%, 적중 {r['hit_target']})")

    print("\n=== 실제 매매 성과 (portfolio_history) ===")
    perf = get_portfolio_performance()
    p_stats = perf.get("stats", {})
    if perf.get("error"):
        print(f"[조회 실패] {perf['error']}")
    else:
        print(f"거래 {p_stats.get('total_trades', 0)}건 | 승률 {p_stats.get('win_rate', 0)}% | "
              f"평균수익 {p_stats.get('avg_return', 0)}% | 누적손익 {p_stats.get('total_pnl', 0):,}원")

    n = r_stats.get("valid", 0) + p_stats.get("total_trades", 0)
    print(f"\n표본 합계 {n}건.")
    if n < _MIN_SAMPLE_FOR_SIGNAL:
        print(
            f"⚠️ {_MIN_SAMPLE_FOR_SIGNAL}건 미만 — 위 승률/평균수익은 우연과 구분 안 됨. "
            "로직 변경의 근거로 쓰지 말고, '과거 사례가 이번 변경으로 뭐가 달라졌을지'만 "
            "정성적으로 훑어볼 것 (예: 목표가 산식을 바꾸면 위 종목들의 target_ret이 어떻게 바뀌는지)."
        )
    else:
        print("변경 전/후로 이 스크립트를 다시 돌려 승률·평균수익이 개선되는 방향인지 비교할 것.")

    return 0  # 항상 0 — CI 게이트가 아니라 사람이 보는 체크리스트


if __name__ == "__main__":
    sys.exit(main())
