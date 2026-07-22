"""market_data_client 이상치 가드 회귀 테스트.

2026-07-22 사고: 실제로는 반도체 급등 랠리가 진행 중이던 아침에 장전
브리핑이 "KOSPI -4.46%, KOSPI선물 1080.36(-7.18%)"라는 정반대 수치를
내보냈다. 원인 2가지:
1. fetch_kospi200_futures()에 검증이 전혀 없어 인접 두 봉이 어긋난
   이상치가 그대로 통과했다.
2. fetch_kr_index_realtime()의 KOSPI 유효범위 상한이 6,000으로 고정돼
   있어, 2026-07 실제 KOSPI(6,500~9,000대)가 매번 "비정상"으로 걸려
   "실시간" 값이 사실상 항상 일봉 종가(지난 정보)로 대체되고 있었다.
"""
import pandas as pd
import pytest

from clients import market_data_client as mdc


def _hist(closes, dates=None):
    """closes: 오래된→최신 순서 (실제 yfinance history()와 동일한 정렬)."""
    n = len(closes)
    dates = dates or pd.date_range("2026-07-01", periods=n, freq="D")
    return pd.DataFrame(
        {"Close": closes, "High": closes, "Low": closes, "Volume": [0] * n},
        index=pd.DatetimeIndex(dates),
    )


class _FakeTicker:
    def __init__(self, rows):
        self._rows = rows

    def history(self, period="5d", interval="1d"):
        return self._rows


class _MultiIntervalTicker:
    """interval="5m"이면 장중 봉, 아니면 일봉을 반환 (fetch_kr_index_realtime용)."""

    def __init__(self, daily_df, intraday_df):
        self._daily = daily_df
        self._intraday = intraday_df

    def history(self, period="5d", interval="1d"):
        return self._intraday if interval == "5m" else self._daily


def test_kospi200_futures_rejects_extreme_adjacent_bar_glitch(monkeypatch):
    """인접 두 봉이 어긋나 -7.18%급 극단치가 나오면 수집 제외해야 한다."""
    rows = _hist([350.12, 1080.36])  # prev=350.12, latest=1080.36 → +208%
    monkeypatch.setattr(mdc.yf, "Ticker", lambda sym: _FakeTicker(rows))
    assert mdc.fetch_kospi200_futures() == {}


def test_kospi200_futures_rejects_nonpositive(monkeypatch):
    rows = _hist([350.0, 0.0])
    monkeypatch.setattr(mdc.yf, "Ticker", lambda sym: _FakeTicker(rows))
    assert mdc.fetch_kospi200_futures() == {}


def test_kospi200_futures_passes_normal_change(monkeypatch):
    rows = _hist([350.0, 355.0])
    monkeypatch.setattr(mdc.yf, "Ticker", lambda sym: _FakeTicker(rows))
    result = mdc.fetch_kospi200_futures()
    assert result and result["close"] == 355.0


def test_kr_index_realtime_accepts_2026_kospi_levels(monkeypatch):
    """2026-07 실제 KOSPI(6,500~9,000대)가 '비정상'으로 걸려 실시간 값이
    일봉 종가로만 대체되던 버그의 회귀 테스트 — 상한을 넓혀 통과해야 한다."""
    kospi = _MultiIntervalTicker(_hist([6542.07, 6748.0]), _hist([6748.0]))
    kosdaq = _MultiIntervalTicker(_hist([845.0, 850.0]), _hist([850.0]))

    def fake_ticker(sym):
        return {"^KS11": kospi, "^KQ11": kosdaq}[sym]

    monkeypatch.setattr(mdc.yf, "Ticker", fake_ticker)
    result = mdc.fetch_kr_index_realtime()
    assert result["kospi"]["current"] == 6748.0
    assert result["kospi"]["change_pct"] == pytest.approx(
        (6748.0 - 6542.07) / 6542.07 * 100, abs=0.01
    )


def test_kr_index_realtime_still_rejects_genuine_glitch(monkeypatch):
    """새 범위를 넓혀도 명백한 이상치(자릿수 오류 등)는 여전히 걸러야 한다."""
    kospi = _MultiIntervalTicker(_hist([6542.07, 6748.0]), _hist([65420.7]))
    kosdaq = _MultiIntervalTicker(_hist([845.0, 850.0]), _hist([850.0]))

    def fake_ticker(sym):
        return {"^KS11": kospi, "^KQ11": kosdaq}[sym]

    monkeypatch.setattr(mdc.yf, "Ticker", fake_ticker)
    result = mdc.fetch_kr_index_realtime()
    # 장중 글리치(65420.7)는 버려지고 일봉 종가(6748.0)로 대체돼야 한다
    assert result["kospi"]["current"] == 6748.0
