"""_track_sequential() 무변화 감지 회귀 테스트 (2026-09-11 추가).

market_intelligence_team/issue_stock_agent가 2026-09-10 병렬→순차 전환으로
_parallel()의 자동 "결과 미반영" 감지(branch:{name} job_ledger 기록)를 잃었던
사각지대를 다시 메웠는지 확인한다. 상세 배경은 graph/investment_graph.py의
_track_sequential() 주석 참조.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text

from db.database import init_db, get_conn
import graph.investment_graph as ig

_KST = ZoneInfo("Asia/Seoul")


def _last_job_status(name: str):
    today = datetime.now(_KST).strftime("%Y-%m-%d")
    with get_conn() as conn:
        row = conn.execute(
            text(
                "SELECT status FROM job_runs WHERE date=:d AND job_name=:j "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"d": today, "j": name},
        ).fetchone()
    return row[0] if row else None


def test_records_success_when_field_actually_changes():
    init_db()
    wrapped = ig._track_sequential(
        lambda s: {**s, "x_report": "새 결과"}, "unit_test_changed", "x_report"
    )
    wrapped({"x_report": ""})
    assert _last_job_status("branch:unit_test_changed") == "success"


def test_records_fail_when_field_stays_empty():
    """예외 없이 그냥 빈 리포트를 반환하는 경우 — 이번에 메운 사각지대의 핵심 케이스."""
    init_db()
    wrapped = ig._track_sequential(
        lambda s: {**s, "x_report": ""}, "unit_test_empty", "x_report"
    )
    wrapped({"x_report": ""})
    assert _last_job_status("branch:unit_test_empty") == "fail"


def test_records_fail_when_field_unchanged_from_before():
    """이전 값과 완전히 같은 값을 그대로 반환하는 경우도 '미반영'으로 취급한다."""
    init_db()
    wrapped = ig._track_sequential(
        lambda s: {**s, "x_report": "이전값"}, "unit_test_stale", "x_report"
    )
    wrapped({"x_report": "이전값"})
    assert _last_job_status("branch:unit_test_stale") == "fail"


def test_market_intelligence_and_issue_stock_nodes_are_wrapped():
    """실수로 다시 맨 함수(node_intelligence = lambda state: ...run(state))로
    되돌리면 이 테스트가 실패해 사각지대 재발을 알린다."""
    assert ig.node_intelligence.__name__ == "wrapper"
    assert ig.node_issue_stocks.__name__ == "wrapper"


def test_wrapper_propagates_exceptions():
    """run_fn이 예외를 던지면 감지 로직이 삼키지 말고 그대로 올려야 한다
    (그래프 차원의 실패 처리에 맡긴다 — _parallel()과는 성격이 다름)."""
    def _boom(state):
        raise RuntimeError("의도된 테스트 예외")

    wrapped = ig._track_sequential(_boom, "unit_test_boom", "x_report")
    try:
        wrapped({"x_report": ""})
        assert False, "예외가 삼켜짐 — 그대로 전파돼야 한다"
    except RuntimeError:
        pass
