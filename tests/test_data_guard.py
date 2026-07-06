"""데이터 가드 — 오염 수치가 LLM에 도달하기 전에 제거되는지 검증.

여기 담긴 케이스들은 전부 실제로 텔레그램 브리핑에 나갔던 사고들이다.
이 테스트가 깨지면 같은 사고가 재발할 수 있는 상태라는 뜻이다.
"""
from services.data_guard import sanitize_stock_data


def test_normal_data_untouched():
    data = {
        "code": "033780", "name": "KT&G",
        "price": 175100, "per": 19.5, "pbr": 2.0, "roe": 10.0,
        "debt_ratio": 57.5, "op_margin": 21.4, "revenue_growth": 5.5,
        "dividend_yield": 3.2, "market_cap_억": 240000,
        "52w_high": 190000, "52w_low": 110000,
    }
    cleaned, warnings = sanitize_stock_data(dict(data))
    assert warnings == []
    for k, v in data.items():
        assert cleaned[k] == v


def test_dividend_yield_291_percent_removed():
    # 2026-07-05 사고: yfinance 값 ×100 중복 → "배당수익률 291%"
    cleaned, warnings = sanitize_stock_data(
        {"code": "X", "name": "HDV", "dividend_yield": 291.0}
    )
    assert cleaned["dividend_yield"] is None
    assert len(warnings) == 1


def test_quarterly_vs_annual_growth_removed():
    # 2026-07-05 사고: 분기 매출을 연간과 비교 → 전 종목 -60~-75%
    cleaned, _ = sanitize_stock_data(
        {"code": "X", "name": "테스트", "revenue_growth": -75.3}
    )
    # -75.3%는 허용범위(-90~300) 안이므로 개별 필드로는 통과한다 —
    # 이 사고의 진짜 방어선은 valuation_service의 연간끼리 비교 로직이며
    # (test_valuation_service.py), 가드는 -90% 미만의 극단값만 자른다.
    assert cleaned["revenue_growth"] == -75.3

    cleaned2, warnings2 = sanitize_stock_data(
        {"code": "X", "name": "테스트", "revenue_growth": -97.0}
    )
    assert cleaned2["revenue_growth"] is None
    assert warnings2


def test_impossible_per_removed():
    cleaned, _ = sanitize_stock_data({"code": "X", "name": "T", "per": 99999})
    assert cleaned["per"] is None


def test_52w_band_inconsistent_with_price_removed():
    # 삼성전자 사례: 현재가 309,500 vs 52주 저점 59,800 (5.2배) —
    # 액면분할·소스 혼선 의심. 남겨두면 "저점 대비 상승 여력 큼" 오판 유발.
    cleaned, warnings = sanitize_stock_data({
        "code": "005930", "name": "삼성전자",
        "price": 309500, "52w_high": 330000, "52w_low": 59800,
    })
    assert cleaned["52w_high"] is None
    assert cleaned["52w_low"] is None
    assert any("모순" in w for w in warnings)


def test_price_above_52w_high_removed():
    cleaned, _ = sanitize_stock_data({
        "code": "X", "name": "T",
        "price": 200000, "52w_high": 150000, "52w_low": 100000,
    })
    assert cleaned["52w_high"] is None and cleaned["52w_low"] is None


def test_non_numeric_value_removed():
    cleaned, warnings = sanitize_stock_data({"code": "X", "name": "T", "per": "N/A값"})
    assert cleaned["per"] is None
    assert warnings
