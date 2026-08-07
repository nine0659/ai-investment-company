"""병렬 L2/L3 노드 state 병합 회귀 테스트 — 2026-08-07 발견된 핵심 파이프라인 버그.

병렬 에이전트들이 각자 `state[k]=v; return state`로 전체 state를 반환하면
LangGraph의 _last 리듀서가 "가장 나중에 병합된 형제 브랜치"의 (그 브랜치
입장에서는 안 바뀐, 즉 옛) 값으로 다른 형제가 방금 써넣은 변경사항을
덮어써버린다. 2026-06-12 병렬 파이프라인 도입 이후 매크로·빅피겨·뉴스·
글로벌인텔리전스·이벤트리스크·이슈종목 분석이 거의 매번 서로를 지워
심층 리포트·CEO 브리핑 어디에도 도달하지 못했다 — 계산은 됐지만(OpenAI
비용도 발생) 결과가 반영되기 직전에 증발.

graph/investment_graph.py의 node_futures 등 L2/L3 래퍼가 `_delta()`로
실제 변경분만 반환하도록 고쳤다. 이 테스트는 모든 에이전트를 목업으로
바꿔치기해 실제 네트워크/LLM/텔레그램 호출 없이 그래프 병합 동작 자체를
검증한다.
"""
import graph.investment_graph as ig


def _set_field(field, value):
    def _run(state):
        state[field] = value
        return state
    return _run


def _make_initial_state():
    return {
        "run_type": "pre_market", "timestamp": "", "date": "2026-08-07",
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


def test_all_parallel_branch_reports_survive_to_final_state(monkeypatch):
    """L2(7개)+L3(2개) 병렬 브랜치가 각자 쓴 필드가 전부 최종 state에 남아있어야 한다."""
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

    expectations = {
        "futures_report":             "선물테스트",
        "us_market_report":           "미국테스트",
        "news_report":                "뉴스테스트",
        "bigfigure_report":           "빅피겨테스트",
        "macro_report":               "매크로테스트",
        "event_risk_report":          "이벤트테스트",
        "market_intelligence_report": "인텔테스트",
        "money_flow_report":          "수급테스트",
        "issue_stocks_report":        "이슈종목테스트",
        "risk_report":                "리스크테스트",
        "committee_report":           "위원회테스트",
        "portfolio_report":           "포트폴리오테스트",
        "midterm_stock_report":       "중기테스트",
    }

    monkeypatch.setattr(futures_market_team, "run", _set_field("futures_report", expectations["futures_report"]))
    monkeypatch.setattr(us_global_team, "run", _set_field("us_market_report", expectations["us_market_report"]))
    monkeypatch.setattr(news_analysis_team, "run", _set_field("news_report", expectations["news_report"]))
    monkeypatch.setattr(bigfigure_agent, "run", _set_field("bigfigure_report", expectations["bigfigure_report"]))
    monkeypatch.setattr(macro_team, "run", _set_field("macro_report", expectations["macro_report"]))
    monkeypatch.setattr(event_risk_team, "run", _set_field("event_risk_report", expectations["event_risk_report"]))
    monkeypatch.setattr(market_intelligence_team, "run", _set_field("market_intelligence_report", expectations["market_intelligence_report"]))
    monkeypatch.setattr(korea_flow_team, "run", _set_field("money_flow_report", expectations["money_flow_report"]))
    monkeypatch.setattr(issue_stock_agent, "run", _set_field("issue_stocks_report", expectations["issue_stocks_report"]))
    monkeypatch.setattr(risk_management_team, "run", _set_field("risk_report", expectations["risk_report"]))
    monkeypatch.setattr(investment_committee, "run", _set_field("committee_report", expectations["committee_report"]))
    monkeypatch.setattr(portfolio_manager_agent, "run", _set_field("portfolio_report", expectations["portfolio_report"]))
    monkeypatch.setattr(midterm_stock_agent, "run", _set_field("midterm_stock_report", expectations["midterm_stock_report"]))

    def _ceo_run(state):
        state["ceo_report"] = "CEO테스트"
        state["ceo_decisions"] = {}
        return state
    monkeypatch.setattr(ceo_agent, "run", _ceo_run)

    monkeypatch.setattr(telegram_client, "send_message", lambda *a, **k: None)
    monkeypatch.setattr(telegram_client, "send_error_alert", lambda *a, **k: None)
    monkeypatch.setattr(nav_service, "record_nav", lambda *a, **k: None)
    monkeypatch.setattr(ig, "collect_raw_data", lambda state: state)

    graph = ig.build_graph()
    final = graph.invoke(_make_initial_state())

    for field, expected in expectations.items():
        assert final.get(field) == expected, f"{field} 유실됨 — 병렬 병합 버그 재발"


def test_errors_from_parallel_branches_do_not_explode(monkeypatch, caplog):
    """병렬 브랜치가 담은 오류는 state["errors"]로 전파하지 않고 로그로만 남긴다.

    처음엔 "새로 늘어난 만큼만 슬라이스해서 반환"하는 방식을 시도했으나, L2/L3처럼
    여러 병렬 브랜치가 동시에 같은 operator.add 채널에 값을 반환하면 병합 과정에서
    지수적으로 중복되는 것을 실측 확인(오류 1건 → 최종 512건). 안전한 반환 방식을
    못 찾아 병렬 구간의 errors는 리듀서에 아예 넘기지 않기로 했다 — 이 테스트는
    그 안전장치(폭증하지 않음)와 본래 델타 로직(리포트 필드는 정상 반영됨)을
    함께 검증한다.
    """
    import agents.futures_market_team as futures_market_team
    import agents.macro_team as macro_team
    import agents.us_global_team as us_global_team
    import agents.news_analysis_team as news_analysis_team
    import agents.bigfigure_agent as bigfigure_agent
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

    def _err(field, msg):
        def _run(state):
            state[field] = "ok"
            state["errors"].append(msg)
            return state
        return _run

    monkeypatch.setattr(futures_market_team, "run", _err("futures_report", "futures_err"))
    monkeypatch.setattr(macro_team, "run", _err("macro_report", "macro_err"))
    for mod, field in [
        (us_global_team, "us_market_report"), (news_analysis_team, "news_report"),
        (bigfigure_agent, "bigfigure_report"), (event_risk_team, "event_risk_report"),
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
        return state
    monkeypatch.setattr(ceo_agent, "run", _ceo_run)

    monkeypatch.setattr(telegram_client, "send_message", lambda *a, **k: None)
    monkeypatch.setattr(telegram_client, "send_error_alert", lambda *a, **k: None)
    monkeypatch.setattr(nav_service, "record_nav", lambda *a, **k: None)
    monkeypatch.setattr(ig, "collect_raw_data", lambda state: state)

    graph = ig.build_graph()
    with caplog.at_level("WARNING"):
        final = graph.invoke(_make_initial_state())

    # 폭증하지 않아야 함 (병렬 구간 errors는 리듀서에 안 넘어감 — 설계된 동작)
    assert len(final["errors"]) < 10
    # 대신 로그로는 남아있어야 함 (진단 정보 유실 아님)
    assert any("futures_err" in r.message for r in caplog.records)
    assert any("macro_err" in r.message for r in caplog.records)
    # 오류가 나도 그 브랜치의 리포트 필드 자체는 정상 반영돼야 함 (델타 로직 검증)
    assert final["futures_report"] == "ok"
    assert final["macro_report"] == "ok"
