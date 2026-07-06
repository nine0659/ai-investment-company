"""NAV 리포트 — 기준점 불일치 알파 버그(2026-07-05) 재발 방지.

당시 사고: 포트폴리오는 7/2 추적 시작 기준, KOSPI는 1월 연초 기준으로
빼서 '알파 -71%' 같은 무의미한 수치가 나갔고, 포트폴리오 수치는 첫
기록값에 고정되어 주간 변화가 항상 +0.00%였다.
"""
import sys
import types

import pytest

sys.modules.setdefault("clients.kis_client", types.SimpleNamespace(KISClient=object))

from services import nav_service


HISTORY = [
    {"date": "2026-06-29", "total_value": 100_000_000, "total_pnl_pct": 10.0,
     "kospi_pct_ytd": 80.0, "nav_pct_ytd": 0.0, "alpha_ytd": 0.0, "kospi_close": 4000.0},
    {"date": "2026-07-03", "total_value": 102_000_000, "total_pnl_pct": 12.0,
     "kospi_pct_ytd": 87.7, "nav_pct_ytd": 2.0, "alpha_ytd": 5.65, "kospi_close": 3854.0},
]


@pytest.fixture
def stub_history(monkeypatch):
    monkeypatch.setattr(nav_service, "get_nav_history", lambda days=7: HISTORY)


def test_alpha_uses_same_window_for_both_sides(stub_history):
    report = nav_service.generate_nav_report(days=7)
    # 포트폴리오 +2.00%p, KOSPI (3854-4000)/4000 = -3.65% → 알파 +5.65%p
    assert "+2.00%" in report
    assert "-3.65%" in report
    assert "+5.65%p" in report


def test_no_mismatched_ytd_comparison(stub_history):
    # '연초대비' 표기는 폐기 — 포트폴리오 추적 시작(7/2)과 KOSPI 연초(1월)를
    # 섞어 비교하던 관행이 -71% 알파의 원인이었다
    report = nav_service.generate_nav_report(days=7)
    assert "연초대비" not in report


def test_cumulative_pnl_labeled_as_cumulative(stub_history):
    report = nav_service.generate_nav_report(days=7)
    assert "누적" in report


def test_empty_history_returns_empty(monkeypatch):
    monkeypatch.setattr(nav_service, "get_nav_history", lambda days=7: [])
    assert nav_service.generate_nav_report(days=7) == ""


def test_zero_kospi_close_no_crash(monkeypatch):
    broken = [dict(h, kospi_close=0) for h in HISTORY]
    monkeypatch.setattr(nav_service, "get_nav_history", lambda days=7: broken)
    report = nav_service.generate_nav_report(days=7)
    assert "0.00%" in report  # KOSPI 결측 시 0 처리, 크래시 없음
