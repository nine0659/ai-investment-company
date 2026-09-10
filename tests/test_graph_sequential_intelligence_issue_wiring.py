"""market_intelligence_team/issue_stock_agent 순차 이동 회귀 테스트 (2026-09-10).

두 에이전트는 원래 각각 L2/L3 병렬 브랜치였는데, market_intelligence_team은
같은 L2 형제인 bigfigure_agent/macro_team의 결과를, issue_stock_agent는 같은
L3 형제인 korea_flow_team의 sector_report를 프롬프트에 참조하도록 설계돼
있었다. 병렬 형제끼리는 barrier를 통과하기 전엔 서로의 결과를 볼 수 없어서
이 참조는 항상 빈 값이었다(코드 자체는 3개월 넘게 정상 실행됐지만 의도한
교차검증 컨텍스트가 죽어있었음). l2_barrier/l3_barrier 이후 순차 노드로
옮겨서 고쳤다 — 이 테스트는 "실제로 형제의 결과를 받는지"를 목(mock)이 아닌
실제 run() 함수로 검증해, 누군가 실수로 다시 병렬 레이어에 넣어도 잡아낸다.
"""
import agents.market_intelligence_team as market_intelligence_team
import agents.issue_stock_agent as issue_stock_agent
import graph.investment_graph as ig


def _set_field(field, value):
    def _run(state):
        state[field] = value
        state["errors"] = []
        return state
    return _run


def _make_initial_state():
    return {
        "run_type": "pre_market", "timestamp": "", "date": "2026-09-10",
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


def test_market_intelligence_team_receives_sibling_l2_reports(monkeypatch):
    """bigfigure_agent/macro_team이 채운 값이 market_intelligence_team의 실제
    chat() 컨텍스트에 들어가는지 — 병렬이었다면 항상 빈 값이라 이 섹션 자체가
    안 붙었을 것."""
    import agents.futures_market_team as futures_market_team
    import agents.us_global_team as us_global_team
    import agents.news_analysis_team as news_analysis_team
    import agents.bigfigure_agent as bigfigure_agent
    import agents.macro_team as macro_team
    import agents.event_risk_team as event_risk_team
    import agents.korea_flow_team as korea_flow_team
    import agents.risk_management_team as risk_management_team
    import agents.investment_committee as investment_committee
    import agents.portfolio_manager_agent as portfolio_manager_agent
    import agents.midterm_stock_agent as midterm_stock_agent
    import agents.ceo_agent as ceo_agent
    import clients.telegram_client as telegram_client
    import services.nav_service as nav_service
    import services.deep_report_service as deep_report_service
    import agents.bull_bear_debate_team as bull_bear_debate_team

    monkeypatch.setattr(deep_report_service, "chat", lambda *a, **k: "요약테스트")
    monkeypatch.setattr(bull_bear_debate_team, "run_bull", _set_field("bull_case_report", "ok"))
    monkeypatch.setattr(bull_bear_debate_team, "run_bear", _set_field("bear_case_report", "ok"))

    monkeypatch.setattr(futures_market_team, "run", _set_field("futures_report", "ok"))
    monkeypatch.setattr(us_global_team, "run", _set_field("us_market_report", "ok"))
    monkeypatch.setattr(news_analysis_team, "run", _set_field("news_report", "ok"))
    monkeypatch.setattr(bigfigure_agent, "run", _set_field("bigfigure_report", "허사비스: AGI 3년 내 가능성 언급"))
    monkeypatch.setattr(macro_team, "run", _set_field("macro_report", "RISK-ON — 금리인하 기대"))
    monkeypatch.setattr(event_risk_team, "run", _set_field("event_risk_report", "ok"))
    monkeypatch.setattr(korea_flow_team, "run", _set_field("money_flow_report", "ok"))
    monkeypatch.setattr(risk_management_team, "run", _set_field("risk_report", "ok"))
    monkeypatch.setattr(investment_committee, "run", _set_field("committee_report", "ok"))
    monkeypatch.setattr(portfolio_manager_agent, "run", _set_field("portfolio_report", "ok"))
    monkeypatch.setattr(midterm_stock_agent, "run", _set_field("midterm_stock_report", "ok"))

    def _ceo_run(state):
        state["ceo_report"] = "ok"
        state["ceo_decisions"] = {}
        state["errors"] = []
        return state
    monkeypatch.setattr(ceo_agent, "run", _ceo_run)

    monkeypatch.setattr(telegram_client, "send_message", lambda *a, **k: None)
    monkeypatch.setattr(telegram_client, "send_error_alert", lambda *a, **k: None)
    monkeypatch.setattr(nav_service, "record_nav", lambda *a, **k: None)
    monkeypatch.setattr(ig, "collect_raw_data", lambda state: state)

    # issue_stock_agent도 이번에 순차로 옮겨 실제 run()이 도는데, 이 테스트의
    # 관심사가 아니므로 그냥 mock 처리(korea_flow_team의 sector_report도 안 채움).
    import agents.issue_stock_agent as _isa
    monkeypatch.setattr(_isa, "run", _set_field("issue_stocks_report", "ok"))

    # market_intelligence_team.py가 `from clients.X import fetch_Y` 형태로 임포트하므로
    # 이름이 그 모듈 네임스페이스에 직접 바인딩된다 — clients.X 쪽을 패치하면 안 먹는다.
    monkeypatch.setattr(market_intelligence_team, "fetch_all_intelligence", lambda **k: {})
    monkeypatch.setattr(market_intelligence_team, "fetch_telegram_intelligence", lambda **k: [])
    monkeypatch.setattr(market_intelligence_team, "fetch_securities_reports", lambda **k: [])
    monkeypatch.setattr(market_intelligence_team, "fetch_blog_posts", lambda **k: [])

    captured = {}

    def _fake_chat(system, context, max_tokens=1200):
        captured["context"] = context
        return "인텔리전스결과"

    monkeypatch.setattr(market_intelligence_team, "chat", _fake_chat)

    graph = ig.build_graph()
    final = graph.invoke(_make_initial_state())

    assert final["market_intelligence_report"] == "인텔리전스결과"
    assert "허사비스: AGI 3년 내 가능성 언급" in captured["context"], (
        "market_intelligence_team이 형제 노드 bigfigure_agent의 결과를 못 받음 "
        "— 다시 병렬 레이어로 되돌아갔을 가능성"
    )
    assert "RISK-ON — 금리인하 기대" in captured["context"], (
        "market_intelligence_team이 형제 노드 macro_team의 결과를 못 받음 "
        "— 다시 병렬 레이어로 되돌아갔을 가능성"
    )


def test_issue_stock_agent_receives_sibling_l3_sector_report(monkeypatch):
    """korea_flow_team이 채운 sector_report가 issue_stock_agent의 실제 chat()
    컨텍스트에 들어가는지 — 병렬이었다면 항상 "섹터 데이터 없음"이었을 것."""
    import agents.futures_market_team as futures_market_team
    import agents.us_global_team as us_global_team
    import agents.news_analysis_team as news_analysis_team
    import agents.bigfigure_agent as bigfigure_agent
    import agents.macro_team as macro_team
    import agents.event_risk_team as event_risk_team
    import agents.korea_flow_team as korea_flow_team
    import agents.risk_management_team as risk_management_team
    import agents.investment_committee as investment_committee
    import agents.portfolio_manager_agent as portfolio_manager_agent
    import agents.midterm_stock_agent as midterm_stock_agent
    import agents.ceo_agent as ceo_agent
    import clients.telegram_client as telegram_client
    import services.nav_service as nav_service
    import services.deep_report_service as deep_report_service
    import agents.bull_bear_debate_team as bull_bear_debate_team

    monkeypatch.setattr(deep_report_service, "chat", lambda *a, **k: "요약테스트")
    monkeypatch.setattr(bull_bear_debate_team, "run_bull", _set_field("bull_case_report", "ok"))
    monkeypatch.setattr(bull_bear_debate_team, "run_bear", _set_field("bear_case_report", "ok"))

    monkeypatch.setattr(futures_market_team, "run", _set_field("futures_report", "ok"))
    monkeypatch.setattr(us_global_team, "run", _set_field("us_market_report", "ok"))
    monkeypatch.setattr(news_analysis_team, "run", _set_field("news_report", "ok"))
    monkeypatch.setattr(bigfigure_agent, "run", _set_field("bigfigure_report", "ok"))
    monkeypatch.setattr(macro_team, "run", _set_field("macro_report", "ok"))
    monkeypatch.setattr(event_risk_team, "run", _set_field("event_risk_report", "ok"))

    def _korea_flow_run(state):
        state["money_flow_report"] = "ok"
        state["sector_report"] = "반도체 섹터 강세 — 거래대금 집중"
        state["errors"] = []
        return state
    monkeypatch.setattr(korea_flow_team, "run", _korea_flow_run)

    monkeypatch.setattr(risk_management_team, "run", _set_field("risk_report", "ok"))
    monkeypatch.setattr(investment_committee, "run", _set_field("committee_report", "ok"))
    monkeypatch.setattr(portfolio_manager_agent, "run", _set_field("portfolio_report", "ok"))
    monkeypatch.setattr(midterm_stock_agent, "run", _set_field("midterm_stock_report", "ok"))

    def _ceo_run(state):
        state["ceo_report"] = "ok"
        state["ceo_decisions"] = {}
        state["errors"] = []
        return state
    monkeypatch.setattr(ceo_agent, "run", _ceo_run)

    monkeypatch.setattr(telegram_client, "send_message", lambda *a, **k: None)
    monkeypatch.setattr(telegram_client, "send_error_alert", lambda *a, **k: None)
    monkeypatch.setattr(nav_service, "record_nav", lambda *a, **k: None)
    monkeypatch.setattr(ig, "collect_raw_data", lambda state: state)

    # market_intelligence_team도 순차로 도는데 이 테스트의 관심사가 아니므로 mock.
    import agents.market_intelligence_team as _mit
    monkeypatch.setattr(_mit, "run", _set_field("market_intelligence_report", "ok"))

    captured = {}

    def _fake_chat(system, context, max_tokens=3000):
        captured["context"] = context
        return "이슈종목결과"

    monkeypatch.setattr(issue_stock_agent, "chat", _fake_chat)

    graph = ig.build_graph()
    final = graph.invoke(_make_initial_state())

    assert final["issue_stocks_report"] == "이슈종목결과"
    assert "반도체 섹터 강세 — 거래대금 집중" in captured["context"], (
        "issue_stock_agent가 형제 노드 korea_flow_team의 sector_report를 못 받음 "
        "— 다시 병렬 레이어로 되돌아갔을 가능성"
    )
