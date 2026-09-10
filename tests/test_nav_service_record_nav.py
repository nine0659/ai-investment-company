"""services/nav_service.py record_nav() 회귀 테스트 (2026-09-10).

generate_nav_report()와 _ratio_drawdown_pct()는 이미 2026-07-10에
"total_pnl_pct 단순 차감/비교는 매매로 인한 실현이익을 손실로 둔갑시킨다"는
사고를 겪고 평가배율(value/cost) 비교로 고쳤다. 그런데 record_nav()가 매일
DB에 저장하고 get_latest_nav()로 CEO 마감 브리핑(ceo_agent.py: "Alpha가
음수이면 전략 재검토 신호")에 직접 노출하는 nav_pct_ytd/alpha_ytd는 그
수정이 빠진 채 단순 차감(total_pnl_pct - baseline["pnl_pct"])을 계속 쓰고
있었다 — 같은 사고가 CEO에게 노출되는 가장 핵심적인 지표에서 재현될 수
있었다. 평가배율 비교로 교체했다.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from db.database import get_conn, init_db
from sqlalchemy import text

import services.nav_service as nav_service

_KST = ZoneInfo("Asia/Seoul")


def setup_function(_):
    init_db()
    with get_conn() as conn:
        conn.execute(text("DELETE FROM portfolio_nav"))


def test_record_nav_uses_ratio_chain_not_simple_pnl_subtraction(monkeypatch):
    now = datetime.now(_KST)
    baseline_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    # baseline(추적 시작): 매입 1000 / 평가 1200 (+20%, 수익 종목 포함)
    with get_conn() as conn:
        conn.execute(
            text("""
                INSERT INTO portfolio_nav
                (date, total_value, total_cost, total_pnl, total_pnl_pct,
                 kospi_close, kospi_pct_ytd, nav_pct_ytd, alpha_ytd, position_count)
                VALUES (:date, 1200, 1000, 200, 20.0, 3000, 0, 0, 0, 2)
            """),
            {"date": baseline_date},
        )

    # 오늘: 수익 난 종목을 전량매도(실현)하고 남은 포지션만 있다 — 성과 자체는
    # baseline과 동일 수준(+5%)인데, 매도로 구성이 줄면서 total_pnl_pct는
    # 20%→5%로 크게 떨어진다. 이건 손실이 아니라 이익 실현이다.
    fake_pnl = [{"code": "005930", "name": "삼성전자", "current_val": 630, "invested": 600}]
    import services.portfolio_service as portfolio_service
    monkeypatch.setattr(portfolio_service, "calculate_pnl", lambda kis=None: fake_pnl)

    import yfinance as yf

    class _EmptyHist:
        empty = True

    class _FakeTicker:
        def __init__(self, *a, **k):
            pass

        def history(self, *a, **k):
            return _EmptyHist()

    monkeypatch.setattr(yf, "Ticker", _FakeTicker)

    result = nav_service.record_nav()

    assert result is not None
    # 옛 계산식(단순 차감)이었다면: 5.0 - 20.0 = -15.0% (실현이익을 손실로 오판)
    assert result["nav_pct_ytd"] != -15.0, "단순 pnl% 차감으로 되돌아감 — 2026-07-10과 같은 계열 버그 재발"
    # 새 계산식(평가배율 체인): (630/600) / (1200/1000) - 1 = 1.05/1.2 - 1 = -12.5%
    assert result["nav_pct_ytd"] == -12.5


def test_record_nav_falls_back_to_old_formula_when_baseline_has_no_cost_data(monkeypatch):
    """옛 스키마(total_cost 없는 과거 기록)가 baseline이면 크래시 대신 이전
    방식으로 안전하게 폴백해야 한다."""
    now = datetime.now(_KST)
    baseline_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    with get_conn() as conn:
        conn.execute(
            text("""
                INSERT INTO portfolio_nav
                (date, total_value, total_cost, total_pnl, total_pnl_pct,
                 kospi_close, kospi_pct_ytd, nav_pct_ytd, alpha_ytd, position_count)
                VALUES (:date, 0, 0, 0, 10.0, 3000, 0, 0, 0, 1)
            """),
            {"date": baseline_date},
        )

    fake_pnl = [{"code": "005930", "name": "삼성전자", "current_val": 630, "invested": 600}]
    import services.portfolio_service as portfolio_service
    monkeypatch.setattr(portfolio_service, "calculate_pnl", lambda kis=None: fake_pnl)

    import yfinance as yf

    class _EmptyHist:
        empty = True

    class _FakeTicker:
        def __init__(self, *a, **k):
            pass

        def history(self, *a, **k):
            return _EmptyHist()

    monkeypatch.setattr(yf, "Ticker", _FakeTicker)

    result = nav_service.record_nav()

    assert result is not None
    # 폴백: total_pnl_pct(5.0) - baseline pnl_pct(10.0) = -5.0
    assert result["nav_pct_ytd"] == -5.0
