"""L2/L3 병렬 브랜치 단위 실행 기록 — job 전체는 성공해도 특정 브랜치만
계속 실패/무반영이면 job 단위 daily_health로는 못 잡는다(2026-08 발견,
3bdbb8d 버그가 2개월간 이 사각지대에서 진행됐다). _parallel()이 브랜치별로
job_runs에 branch:<name> 레코드를 남기고, job_ledger가 이를 집계해
daily_health 문제 목록에 포함시키는지 검증한다.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from db.database import init_db, get_conn
from sqlalchemy import text

import graph.investment_graph as ig
from services import job_ledger

# record_job()은 state["date"]가 아니라 실행 시각(KST 오늘)을 그대로 쓴다 —
# 테스트도 하드코딩된 날짜 대신 실제 오늘 날짜로 조회해야 어느 날 돌려도 맞다.
_TODAY = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")


def _set_field(field, value):
    def _run(state):
        state[field] = value
        return state
    return _run


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


def _run_minimal_graph(monkeypatch, futures_ok: bool):
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

    def _futures_run(state):
        if futures_ok:
            state["futures_report"] = "ok"
        else:
            state["errors"].append("futures_broken")  # 필드는 안 바꾸고 오류만 냄
        return state
    monkeypatch.setattr(futures_market_team, "run", _futures_run)

    for mod, field in [
        (us_global_team, "us_market_report"), (news_analysis_team, "news_report"),
        (bigfigure_agent, "bigfigure_report"), (macro_team, "macro_report"),
        (event_risk_team, "event_risk_report"), (market_intelligence_team, "market_intelligence_report"),
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
    return graph.invoke(_make_initial_state())


def setup_function(_):
    init_db()
    with get_conn() as conn:
        conn.execute(text("DELETE FROM job_runs WHERE job_name LIKE 'branch:%'"))


def test_failing_branch_recorded_as_fail(monkeypatch):
    _run_minimal_graph(monkeypatch, futures_ok=False)
    with get_conn() as conn:
        row = conn.execute(
            text("SELECT status, detail FROM job_runs WHERE job_name='branch:futures_market_team' "
                 "ORDER BY id DESC LIMIT 1")
        ).fetchone()
    assert row is not None
    assert row[0] == "fail"
    assert "futures_broken" in row[1]


def test_succeeding_branch_recorded_as_success(monkeypatch):
    _run_minimal_graph(monkeypatch, futures_ok=True)
    with get_conn() as conn:
        row = conn.execute(
            text("SELECT status FROM job_runs WHERE job_name='branch:futures_market_team' "
                 "ORDER BY id DESC LIMIT 1")
        ).fetchone()
    assert row is not None
    assert row[0] == "success"


def test_branch_failures_surface_in_daily_health_problems(monkeypatch):
    _run_minimal_graph(monkeypatch, futures_ok=False)
    problems = job_ledger.get_yesterday_branch_problems(_TODAY)
    assert any("branch:futures_market_team" in p for p in problems)


def test_branch_records_excluded_from_generic_job_failure_list(monkeypatch):
    # branch: 레코드는 get_yesterday_branch_problems 전용 — 기존 잡 실패
    # 조회(job_name NOT LIKE 'branch:%')에 'futures_market_team'이라는 접두사 없는
    # 이름으로는 안 남아야 한다 (다른 테스트가 남긴 무관한 testjob_* 행과는
    # 공유 DB라 섞일 수 있으므로, 테이블 전체가 비어있길 기대하지 않는다).
    _run_minimal_graph(monkeypatch, futures_ok=False)
    with get_conn() as conn:
        rows = conn.execute(
            text("SELECT job_name FROM job_runs WHERE date=:d AND status='fail' "
                 "AND job_name = 'futures_market_team'"),
            {"d": _TODAY},
        ).fetchall()
    assert rows == []
