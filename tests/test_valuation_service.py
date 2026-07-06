"""밸류에이션 계산 — 분기/연간 혼합 비교 버그(2026-07-05) 재발 방지.

당시 사고: 당해 1분기(3개월) 매출을 직전 연간(12개월)과 비교해
전 종목 매출성장률이 -60~-75%로 찍혔고, LLM이 모든 종목에
"성장성 부진"을 쓰면서 안정마진 종목(KT&G)만 반복 추천됐다.
"""
import sys
import types

import pytest

# KIS 클라이언트는 네트워크 의존 — 모듈 로드 전에 스텁으로 대체
sys.modules.setdefault("clients.kis_client", types.SimpleNamespace(KISClient=object))

from services import valuation_service as vs


class FakeKIS:
    def get_stock_price(self, code):
        return {"price": 175100, "per": 19.5, "pbr": 2.0, "market_cap_억": 240000,
                "52w_high": 190000, "52w_low": 110000}

    def get_dividend_info(self, code):
        return {"dividend_yield": 3.2}


HISTORY_WITH_QUARTER = [
    {"year": 2026, "period": "1분기", "매출액": 1_500_000_000_000,
     "영업이익": 320_000_000_000, "당기순이익": 250_000_000_000,
     "자본총계": 10_000_000_000_000, "부채총계": 5_760_000_000_000},
    {"year": 2025, "period": "연간", "매출액": 5_800_000_000_000,
     "영업이익": 1_240_000_000_000, "당기순이익": 980_000_000_000,
     "자본총계": 9_800_000_000_000, "부채총계": 5_640_000_000_000},
    {"year": 2024, "period": "연간", "매출액": 5_500_000_000_000,
     "영업이익": 1_150_000_000_000, "당기순이익": 900_000_000_000,
     "자본총계": 9_300_000_000_000, "부채총계": 5_400_000_000_000},
]


@pytest.fixture
def stub_dart(monkeypatch):
    monkeypatch.setattr(
        vs, "dart_client",
        types.SimpleNamespace(get_multi_year_financials=lambda c, y: HISTORY_WITH_QUARTER),
    )


def test_revenue_growth_compares_annual_to_annual(stub_dart):
    r = vs.get_stock_valuation(FakeKIS(), "033780", "KT&G", years=3)
    # 연간끼리: (5.8조 - 5.5조) / 5.5조 = +5.45% — 분기 혼합이면 -74%가 나온다
    assert r["revenue_growth"] == pytest.approx(5.45, abs=0.01)
    assert r["revenue_growth"] > 0


def test_roe_uses_annual_income(stub_dart):
    r = vs.get_stock_valuation(FakeKIS(), "033780", "KT&G", years=3)
    # 연간 순이익 9,800억 / 자본 9.8조 = 10.0% (분기 순이익으로 계산하면 2.5%)
    assert r["roe"] == pytest.approx(10.0, abs=0.1)


def test_quarterly_separated_into_own_fields(stub_dart):
    r = vs.get_stock_valuation(FakeKIS(), "033780", "KT&G", years=3)
    assert r["q_label"] == "2026 1분기"
    assert r["q_revenue_억"] == 15_000
    assert r["q_op_margin"] == pytest.approx(21.33, abs=0.01)


def test_prompt_labels_annual_and_quarterly(stub_dart):
    r = vs.get_stock_valuation(FakeKIS(), "033780", "KT&G", years=3)
    text = vs.format_for_prompt(r)
    assert "매출성장률(연간" in text
    assert "최근분기(2026 1분기)" in text


def test_corrupted_kis_dividend_blocked(stub_dart):
    class BadKIS(FakeKIS):
        def get_dividend_info(self, code):
            return {"dividend_yield": 291.0}  # 단위 오류 오염 데이터

    r = vs.get_stock_valuation(BadKIS(), "033780", "KT&G", years=3)
    assert r["dividend_yield"] is None
    assert r["_data_warnings"]


def test_annual_only_history_still_works(stub_dart, monkeypatch):
    annual_only = [h for h in HISTORY_WITH_QUARTER if h["period"] == "연간"]
    monkeypatch.setattr(
        vs, "dart_client",
        types.SimpleNamespace(get_multi_year_financials=lambda c, y: annual_only),
    )
    r = vs.get_stock_valuation(FakeKIS(), "033780", "KT&G", years=3)
    assert r["revenue_growth"] == pytest.approx(5.45, abs=0.01)
    assert "q_label" not in r or r.get("q_label") is None
