"""scripts/judgment_scenario_check.py

CEO/에이전트의 "판단"에 영향을 주는 로직(프롬프트, 가드, 파싱, 그래프 배선)을
바꾸기 전에 훑어보는 사람용 체크리스트.

배경: 지금 있는 테스트(tests/, 48개)는 전부 "과거에 실제로 터진 사고가 또
재현되지 않는지"를 확인하는 회귀 테스트다(CLAUDE.md 참조). 반면 이 스크립트는
"이 시스템이 실제로 마주칠 만한 대표 상황들에서, 여전히 말이 되는 판단을
내리는가"를 사람이 한 번 점검하는 용도다 — scripts/backtest_gate_check.py가
"추천 성과"를 점검하는 것과 같은 철학을, "판단 로직 전반"으로 넓힌 것.

CI에 안 물렸다. 자동 pass/fail이 아니라, 로직을 바꾸기 전/후로 사람이 훑어보는
체크리스트다 — 무엇을 확인해야 하는지 잊지 않기 위한 목록이라고 보면 된다.

사용법:
    python scripts/judgment_scenario_check.py

각 항목의 "회귀테스트"가 ✅면 이미 자동으로 지켜지고 있으니 안심해도 되고,
⚠️면 지금은 사람이 직접 확인하는 수밖에 없다는 뜻이다 — 로직을 바꿀 때
특히 신경 써서 봐야 한다.
"""
import sys

SCENARIOS = [
    dict(
        title="드로다운이 오판(데이터 오류)으로 크게 잡히는 상황",
        situation="KIS 시세 일부 누락 등으로 실제로는 안 그런데 NAV가 -40% 이상 급락한 것처럼 보임",
        check="자동매도가 절대 실행되지 않고, 경보만 발송되는지",
        ref="services/nav_service.check_drawdown_defense / 2026-07-08 실계좌 전량청산 시도 사고",
        tested="✅ tests/test_drawdown_policy.py",
    ),
    dict(
        title="CEO가 스스로 정한 비중 규칙을 어기는 상황",
        situation="확신도 high인데 신규 제안 비중이 10%를 초과 (규정 밴드 5~10%)",
        check="발송은 막지 않되 리스크 게이트 경고 메시지가 별도로 뜨는지",
        ref="services/risk_gate.py",
        tested="✅ tests/test_risk_gate.py",
    ),
    dict(
        title="한국 시장 대리지표끼리 방향이 어긋나는데 고신뢰 액션이 겹치는 상황",
        situation="KOSPI 실시간과 EWY(한국 ETF)가 반대 방향으로 3%p 이상 벌어진 날, CEO가 전량청산·20%p 이상 비중변경을 권고",
        check="발송 자체가 보류되고 사람 확인을 요구하는지 (2026-07-22 KOSPI 오염 사고 재현 여부)",
        ref="services/decision_guard.py",
        tested="✅ tests/test_decision_guard.py, test_decision_guard_wiring.py",
    ),
    dict(
        title="재무 데이터의 분기/연간이 섞이는 상황",
        situation="DART get_multi_year_financials의 첫 값이 분기 보고서인 종목",
        check="분기(3개월)와 연간(12개월) 손익이 섞여 성장률이 계산되지 않는지",
        ref="services/valuation_service.py / 2026-06 전종목 성장률 -75% 사고",
        tested="⚠️ 사람이 직접 확인 — 전용 회귀 테스트 없음",
    ),
    dict(
        title="배당수익률 단위가 이중으로 들어오는 상황",
        situation="yfinance dividendYield가 0.0291(비율) 또는 2.91(퍼센트)로 뒤섞여 들어옴",
        check="_normalize_dividend_yield() 경유 후에도 여전히 0.01~20% 범위로 정규화되는지",
        ref="agents/us_invest_agent.py의 _normalize_dividend_yield / services/data_guard.py 최종 백스톱",
        tested="⚠️ 사람이 직접 확인 — 전용 회귀 테스트 없음",
    ),
    dict(
        title="현재가와 52주 고저가 모순되는 상황",
        situation="액면분할·소스 혼선 등으로 현재가가 52주 저점의 5배를 초과하거나 52주 고점보다 5% 이상 높게 나옴",
        check="52주 고/저 필드가 N/A로 제거되고, 조작된 '상승여력' 서술이 브리핑에 안 나가는지",
        ref="services/data_guard.py sanitize_stock_data",
        tested="✅ tests/test_data_guard.py",
    ),
    dict(
        title="이상치가 광범위하게 몰리는 상황",
        situation="한 번의 브리핑에서 5건 이상 종목 데이터가 범위를 벗어남 (데이터 소스 자체 장애 의심)",
        check="개별 제거뿐 아니라 관리자에게 별도 경보(데이터 소스 점검 필요)가 가는지",
        ref="services/data_guard.py alert_if_widespread",
        tested="⚠️ 사람이 직접 확인 — 전용 회귀 테스트 없음",
    ),
    dict(
        title="DATABASE_URL이 빠진 채로 실행되는 상황",
        situation="운영 환경변수 누락으로 SQLite로 조용히 폴백",
        check="텔레그램 경보가 가는지, 폴백 상태로 쌓인 데이터가 재시작 시 사라진다는 걸 인지하고 있는지",
        ref="db/database.py / 2026-07-02 데이터 소실 사고",
        tested="✅ tests/test_database_env.py",
    ),
    dict(
        title="같은 날 브리핑이 두 경로(Render + GH Actions)에서 동시에 걸리는 상황",
        situation="cron-job.org와 GH Actions 백업이 겹쳐서 같은 run_type이 중복 트리거됨",
        check="claim_report_slot 가드로 한쪽만 발송되고 중복 브리핑이 안 나가는지",
        ref="services/report_service.py claim_report_slot/release_report_slot",
        tested="⚠️ 사람이 직접 확인 — 간접적으로만 커버됨",
    ),
    dict(
        title="종목 발굴이 후보를 못 찾은 주",
        situation="discovery_weekly가 돌았는데 기준을 통과하는 후보가 하나도 없음",
        check="'후보 없음' 같은 공허한 리포트가 나가지 않고 조용히 스킵되는지",
        ref="agents/discovery_agent.py silent_if_empty=True",
        tested="✅ tests/test_discovery_silent_if_empty.py",
    ),
    dict(
        title="병렬 분석 노드 하나가 예외 없이 그냥 빈 리포트를 내는 상황",
        situation="예: macro_team이 크래시는 안 나는데 아무 분석도 못 만들고 빈 문자열 반환",
        check="daily_health가 다음날 branch:macro_team 실패로 잡아내는지",
        ref="graph/investment_graph.py _parallel() / job_ledger.get_yesterday_branch_problems",
        tested="✅ tests/test_graph_parallel_state_merge.py, test_graph_branch_job_ledger.py",
    ),
    dict(
        title="순차로 옮긴 노드(market_intelligence_team/issue_stock_agent)가 형제 결과 없이 도는 상황",
        situation="l2_barrier/l3_barrier 배선이 실수로 다시 병렬로 되돌아가 형제 노드 결과를 못 받음",
        check="컨텍스트가 비어도 daily_health가 branch:market_intelligence_team / branch:issue_stock_agent 실패로 잡아내는지",
        ref="graph/investment_graph.py _track_sequential (2026-09-11 신규 보강)",
        tested="✅ tests/test_graph_sequential_intelligence_issue_wiring.py, test_track_sequential_wiring.py",
    ),
    dict(
        title="텔레그램 승인 큐에서 종목을 연달아 승인하는 상황",
        situation="승인 대기 중인 종목 두 개를 빠르게 연속으로 승인",
        check="먼저 누른 종목의 체결가 대기 슬롯이 나중 종목에 덮여 유실되지 않는지",
        ref="clients/telegram_bot.py / 2026-09 승인큐 슬롯 유실 버그",
        tested="✅ tests/test_telegram_bot_pending_fill_slot.py",
    ),
]


def main() -> int:
    print("=" * 70)
    print("판단 시나리오 체크리스트 — 로직을 바꾸기 전에 훑어볼 것")
    print("=" * 70)
    tested_count = sum(1 for s in SCENARIOS if s["tested"].startswith("✅"))
    print(f"\n총 {len(SCENARIOS)}개 시나리오 — 자동 회귀테스트 있음 {tested_count}개 / "
          f"사람이 직접 확인 필요 {len(SCENARIOS) - tested_count}개\n")

    for i, s in enumerate(SCENARIOS, 1):
        print(f"[{i:02d}] {s['title']}")
        print(f"     상황   : {s['situation']}")
        print(f"     확인할 것: {s['check']}")
        print(f"     근거   : {s['ref']}")
        print(f"     회귀테스트: {s['tested']}")
        print()

    print("-" * 70)
    print("⚠️ 표시된 항목은 지금 바꾸려는 로직과 관련 있다면 특히 손으로 직접")
    print("   확인할 것. 확인이 끝나면 이 파일에 회귀 테스트를 추가해 다음부터는")
    print("   ✅로 바뀌도록 만드는 것이 이 체크리스트의 장기 목표다.")
    print("-" * 70)
    return 0  # 항상 0 — CI 게이트가 아니라 사람이 보는 체크리스트 (backtest_gate_check.py와 동일 철학)


if __name__ == "__main__":
    sys.exit(main())
