"""2026-10-14 회귀: yfinance ^KS11 일봉이 10/13 봉을 통째로 빠뜨려(실제 -5.46%)
장전 브리핑이 10/12 값(-3.65%)을 '전일'로 서술했다. 네이버 값이 더 최신이면 교체한다."""
from clients import market_data_client as m

NAVER_BARS = [
    {"localTradedAt": "2026-10-13", "closePrice": "6,035.35", "compareToPreviousClosePrice": "-348.89",
     "fluctuationsRatio": "-5.46", "highPrice": "6,198.46", "lowPrice": "5,911.11"},
    {"localTradedAt": "2026-10-12", "closePrice": "6,384.24", "compareToPreviousClosePrice": "-241.69",
     "fluctuationsRatio": "-3.65", "highPrice": "6,589.69", "lowPrice": "6,368.49"},
]


def test_bars_parsed_ascending(monkeypatch):
    monkeypatch.setattr(m, "_naver_get", lambda url: NAVER_BARS)
    bars = m.fetch_naver_index_bars("KOSPI")
    assert [b["date"] for b in bars] == ["2026-10-12", "2026-10-13"]
    assert bars[-1]["change_pct"] == -5.46 and bars[-1]["close"] == 6035.35


def test_stale_yfinance_replaced(monkeypatch):
    monkeypatch.setattr(m, "_naver_get", lambda url: NAVER_BARS)
    result = {"kospi": {"close": 6384.24, "change_pct": -3.65, "data_date": "2026-10-12", "symbol": "^KS11"}}
    m._override_stale_kr_index(result)
    assert result["kospi"]["data_date"] == "2026-10-13"
    assert result["kospi"]["change_pct"] == -5.46
    assert result["kospi"]["source"] == "naver"


def test_fresh_yfinance_kept(monkeypatch):
    monkeypatch.setattr(m, "_naver_get", lambda url: NAVER_BARS)
    result = {"kospi": {"close": 6035.35, "change_pct": -5.46, "data_date": "2026-10-13", "symbol": "^KS11"}}
    m._override_stale_kr_index(result)
    assert "source" not in result["kospi"]


def test_naver_down_keeps_yfinance(monkeypatch):
    monkeypatch.setattr(m, "_naver_get", lambda url: None)
    result = {"kospi": {"close": 1, "change_pct": -3.65, "data_date": "2026-10-12"}}
    m._override_stale_kr_index(result)
    assert result["kospi"]["change_pct"] == -3.65


def test_fill_missing_closes_adds_only_missing(monkeypatch):
    monkeypatch.setattr(m, "_naver_get", lambda url: NAVER_BARS)
    out = m.fill_missing_index_closes({"2026-10-12": 6384.0})
    assert out["2026-10-12"] == 6384.0  # 기존 값 유지
    assert out["2026-10-13"] == 6035.35
