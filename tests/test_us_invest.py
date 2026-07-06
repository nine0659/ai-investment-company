"""미국 주식 에이전트 — 배당수익률 291% 오표기(2026-07-05) 재발 방지."""
import pytest

from agents.us_invest_agent import _normalize_dividend_yield, _score_dividend


class TestNormalizeDividendYield:
    def test_fraction_format_converted(self):
        # 구버전 yfinance: 0.0291 → 2.91%
        assert _normalize_dividend_yield(0.0291) == pytest.approx(2.91)

    def test_percent_format_kept(self):
        # 신버전 yfinance: 2.91 → 2.91% (여기에 ×100 하면 291%가 된다)
        assert _normalize_dividend_yield(2.91) == pytest.approx(2.91)

    def test_corrupted_291_percent_dropped(self):
        assert _normalize_dividend_yield(291.0) is None

    def test_none_and_zero(self):
        assert _normalize_dividend_yield(None) is None
        assert _normalize_dividend_yield(0) is None

    def test_negative_dropped(self):
        assert _normalize_dividend_yield(-0.5) is None


def test_score_dividend_uses_percent_directly():
    # dividend_yield는 이미 % 단위 — 3.5%가 배당점수 상한(40) 이내로 반영돼야 함.
    # 과거 버그처럼 ×100이 남아있다면 340점 캡(40)에 걸려 모든 ETF가 동점이 된다.
    low  = _score_dividend({"dividend_yield": 1.0, "change_1m": 0, "change_1w": 0, "vol_ratio": 1.0})
    high = _score_dividend({"dividend_yield": 3.5, "change_1m": 0, "change_1w": 0, "vol_ratio": 1.0})
    assert high > low
    assert high <= 40
