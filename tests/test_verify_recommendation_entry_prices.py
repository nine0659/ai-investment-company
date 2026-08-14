"""scripts/verify_recommendation_entry_prices.py의 날짜 방향 회귀 테스트.

2026-08 첫 실행에서 "이후" 방향으로 종가를 찾다가, 주간 추천이 도는
일요일(비거래일) 날짜 때문에 월요일 종가와 비교해 SK하이닉스가 18.2%
"불일치"로 잘못 잡혔다. 대상 날짜 "이전(포함)" 가장 최근 거래일 종가와
비교해야 KIS get_stock_price()가 그 시점에 실제로 반환했을 값과
맞춰진다 — 방향을 고치자 3건 전부 0.0% 일치로 확인됐다.
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

from scripts.verify_recommendation_entry_prices import _historical_close


class _FakeTicker:
    def __init__(self, symbol):
        self.symbol = symbol

    def history(self, start, end):
        # 7/10(금) 종가만 있고 7/11~7/12(주말)엔 데이터가 없는 상황을 흉내낸다.
        # start~end 윈도우 안에 있으면 그대로 반환(실제 yfinance도 거래일만 반환).
        idx = pd.to_datetime(["2026-07-08", "2026-07-09", "2026-07-10"])
        return pd.DataFrame({"Close": [2_150_000.0, 2_170_000.0, 2_180_000.0]}, index=idx)


def test_uses_most_recent_close_on_or_before_target_date(monkeypatch):
    fake_yf = types.SimpleNamespace(Ticker=_FakeTicker)
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    # 대상 날짜는 일요일(2026-07-12, 비거래일) — 그 이전 마지막 거래일인
    # 7/10 종가(2,180,000)를 가져와야 한다. 만약 "이후" 방향으로 찾았다면
    # 이 가짜 데이터엔 7/12 이후 값이 없어 None이 나왔을 것이다.
    price = _historical_close("000660", "2026-07-12")
    assert price == 2_180_000.0
