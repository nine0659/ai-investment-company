"""job_backtest_snapshot 스케줄러 래퍼 — 무발송 백테스트 스냅샷 기록 검증.

2026-08-18: B그룹(StockBench식 정기 백테스트) 중 리스크 없는 부분만 먼저 착수.
텔레그램 미발송·LLM 미사용이라 claim_report_slot 가드는 필요 없다(CLAUDE.md
절대원칙 4는 "신규 발송 경로"에만 적용). job_runs에 결과가 남는지만 검증한다.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import scheduler
from db.database import get_conn, init_db
from services import job_ledger
from sqlalchemy import text

_KST = ZoneInfo("Asia/Seoul")


def _clear_today_trace():
    today = datetime.now(_KST).strftime("%Y-%m-%d")
    with get_conn() as conn:
        conn.execute(
            text("DELETE FROM job_runs WHERE date=:d AND job_name='backtest_snapshot'"),
            {"d": today},
        )


def test_records_success_trace(monkeypatch):
    init_db()
    _clear_today_trace()
    monkeypatch.setattr(
        "services.backtest_service.get_recommendation_backtest",
        lambda days=20: {"stats": {"valid": 3, "total": 3, "win_rate": 66.7, "avg_return": 4.2}},
    )
    monkeypatch.setattr(
        "services.backtest_service.get_portfolio_performance",
        lambda: {"stats": {"total_trades": 1, "win_rate": 100.0}},
    )

    scheduler.job_backtest_snapshot()

    assert job_ledger.has_trace_today("backtest_snapshot") is True


def test_failure_does_not_raise(monkeypatch):
    """백테스트 계산이 실패해도(yfinance 장애 등) 잡 자체는 예외를 삼켜야 한다 — 무사고 원칙."""
    init_db()
    _clear_today_trace()

    def _boom(days=20):
        raise RuntimeError("yfinance 장애 시뮬레이션")

    monkeypatch.setattr("services.backtest_service.get_recommendation_backtest", _boom)

    scheduler.job_backtest_snapshot()  # 예외가 새어나오면 실패

    with get_conn() as conn:
        row = conn.execute(
            text("SELECT status FROM job_runs WHERE date=:d AND job_name='backtest_snapshot' "
                 "ORDER BY id DESC LIMIT 1"),
            {"d": datetime.now(_KST).strftime("%Y-%m-%d")},
        ).fetchone()
    assert row is not None and row[0] == "fail"
