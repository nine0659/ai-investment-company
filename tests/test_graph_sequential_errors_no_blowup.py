"""순차 노드(L3 이후 risk/review/committee/portfolio/midterm/ceo/save_report)의
errors 필드 지수 폭증 회귀 테스트 — 2026-08-14 발견.

병렬(L2/L3) 브랜치는 3bdbb8d(_parallel() 델타 래퍼)로 이미 고쳤지만, 그 뒤를
잇는 순차 노드들은 여전히 `state["errors"].append(x); return state` 패턴을
쓰고 있었다. `state["errors"]`는 `Annotated[list, operator.add]` 채널의
백업 리스트 객체 그 자체라 직접 append하면 채널의 "이전 값"까지 오염되고,
그 상태로 전체 state를 반환하면 채널 리듀서가 "이전 값 + 이번 기여값"을
더할 때 둘 다 이미 오염된 같은 내용이라 매 노드 hop마다 정확히 2배씩
불어난다(최소 LangGraph 재현으로 실측 확인 — project_langgraph_parallel_state_wipe_bug
메모리 참조. "512건" 사고도 이 메커니즘의 순차 버전일 가능성이 큼).

수정: 각 노드가 새 로컬 리스트(`_new_errors`)에만 모아서 마지막에
`state["errors"] = _new_errors`로 교체 — 원본 리스트를 절대 참조하지 않는
진짜 델타. 이 테스트는 L1에서 오류 1건이 나고 L2~L3~L4(순차 6개+save_report)를
전부 통과해도 최종 errors가 정확히 1건인지 검증한다.
"""
import graph.investment_graph as ig


def _make_initial_state():
    return {
        "run_type": "pre_market", "timestamp": "", "date": "2026-08-14",
        "raw_market_data": {}, "data_freshness": {}, "raw_kis_data": {}, "raw_news_data": {},
        "us_hot_stocks": [], "us_sector_data": {}, "us_52w_highs": [],
        "bigfigure_news": [], "dart_disclosures": [], "kr_index_realtime": {}, "consensus_data": {},
        "weekly_strategy_summary": "", "investment_thesis": "",
        "futures_report": "", "us_market_report": "", "us_impact_report": "",
        "korea_spot_report": "", "global_market_report": "", "news_report": "",
        "bigfigure_report": "", "dart_report": "", "macro_report": "",
        "event_risk_report": "", "event_risk_level": "중간",
        "market_intelligence_report": "", "sector_report": "",
        "issue_stocks_report": "", "midterm_stock_report": "",
        "money_flow_report": "", "risk_report": "", "committee_report": "",
        "portfolio_report": "", "ceo_report": "",
        "candidates": [], "sector_scores": [], "risks": [],
        "risk_level": "중간", "market_direction": "",
        "review_report": "", "errors": [], "nav_recorded": {},
        "ceo_decisions": {}, "deep_report_content": "",
    }


def _set_field(field, value):
    """테스트 목(mock)도 실제 고쳐진 노드와 같은 델타 규약을 지켜야 한다 —
    안 그러면 목 자체가 폭증을 재도입해서 이 테스트가 무의미해진다."""
    def _run(state):
        state[field] = value
        state["errors"] = []  # 이 목은 새 오류를 안 낸다 — 빈 델타
        return state
    return _run


def test_single_early_error_survives_full_pipeline_without_amplification(monkeypatch):
    import agents.futures_market_team as futures_market_team
    import agents.us_global_team as us_global_team
    import agents.news_analysis_team as news_analysis_team
    import agents.bigfigure_agent as bigfigure_agent
    import agents.macro_team as macro_team
    import agents.event_risk_team as event_risk_team
    import agents.market_intelligence_team as market_intelligence_team
    import agents.korea_flow_team as korea_flow_team
    import agents.issue_stock_agent as issue_stock_agent
    import agents.risk_management_team as risk_management_team
    import agents.investment_committee as investment_committee
    import agents.portfolio_manager_agent as portfolio_manager_agent
    import agents.midterm_stock_agent as midterm_stock_agent
    import agents.ceo_agent as ceo_agent
    import clients.telegram_client as telegram_client
    import services.nav_service as nav_service
    import services.report_service as report_service

    # L1에서 오류가 1건 난 것처럼 흉내낸다 — 실제 collect_raw_data와 같은 패턴
    # (수정된 버전)으로 델타만 반환.
    def _collect_with_one_error(state):
        state["errors"] = ["collect_global: 테스트용 오류 1건"]
        return state
    monkeypatch.setattr(ig, "collect_raw_data", _collect_with_one_error)

    for mod, field in [
        (futures_market_team, "futures_report"), (us_global_team, "us_market_report"),
        (news_analysis_team, "news_report"), (bigfigure_agent, "bigfigure_report"),
        (macro_team, "macro_report"), (event_risk_team, "event_risk_report"),
        (market_intelligence_team, "market_intelligence_report"),
    ]:
        monkeypatch.setattr(mod, "run", _set_field(field, "ok"))
    monkeypatch.setattr(korea_flow_team, "run", _set_field("money_flow_report", "ok"))
    monkeypatch.setattr(issue_stock_agent, "run", _set_field("issue_stocks_report", "ok"))
    monkeypatch.setattr(risk_management_team, "run", _set_field("risk_report", "ok"))
    monkeypatch.setattr(investment_committee, "run", _set_field("committee_report", "ok"))
    monkeypatch.setattr(portfolio_manager_agent, "run", _set_field("portfolio_report", "ok"))
    monkeypatch.setattr(midterm_stock_agent, "run", _set_field("midterm_stock_report", "ok"))

    def _ceo_run(state):
        state["ceo_report"] = "ok"
        state["ceo_decisions"] = {}
        state["errors"] = []  # 이 목도 새 오류를 안 낸다 — 빈 델타
        return state
    monkeypatch.setattr(ceo_agent, "run", _ceo_run)

    monkeypatch.setattr(telegram_client, "send_message", lambda *a, **k: None)
    monkeypatch.setattr(telegram_client, "send_error_alert", lambda *a, **k: None)
    monkeypatch.setattr(nav_service, "record_nav", lambda *a, **k: None)
    monkeypatch.setattr(report_service, "save_report", lambda **k: None)

    graph = ig.build_graph()
    final = graph.invoke(_make_initial_state())

    # L1(1건) → L2(7개 병렬, _parallel()이 errors 제외) → L3(2개 병렬, 동일)
    # → risk/review/committee/portfolio/midterm/ceo/save_report(순차 6+1개,
    # 이번에 고친 부분) 를 전부 지나도 정확히 1건이어야 한다.
    # 고치기 전이었다면 2^7~2^8배(128~256건)로 불어났을 것이다.
    assert final["errors"] == ["collect_global: 테스트용 오류 1건"], (
        f"errors 폭증 재발 — 길이 {len(final['errors'])}: {final['errors'][:5]}..."
    )
