"""장중 반전 분석의 반도체 대형주 등락률 조회 회귀 테스트.

2026-09-21 09:30 발송분 사고: KIS 조회가 두 종목 모두 실패해 leaders_text가
통째로 "조회 실패"가 되고, LLM이 "반도체 대형주 등락 데이터 부재로 정확한
대형주 영향 분석 어려움"이라고만 서술한 채 나갔다. KIS 실패 시 yfinance로
폴백하고, 종목별로 실패 여부를 명시하도록 services/alert_service.py를 수정했다.
"""
import services.alert_service as alert_service


class _FakeKIS:
    """get_stock_price가 호출될 때마다 미리 준비된 응답을 순서대로 반환."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get_stock_price(self, code, market=None):
        self.calls += 1
        if not self._responses:
            return {}
        return self._responses.pop(0)


def test_kis_success_first_try_no_retry():
    kis = _FakeKIS([{"price": 72500, "change_pct": 1.23}])
    chg = alert_service._get_reversal_leader_change(kis, "005930", "삼성전자")
    assert chg == 1.23
    assert kis.calls == 1


def test_kis_empty_then_retry_succeeds(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    kis = _FakeKIS([{}, {"price": 72500, "change_pct": 1.23}])
    chg = alert_service._get_reversal_leader_change(kis, "005930", "삼성전자")
    assert chg == 1.23
    assert kis.calls == 2


def test_kis_outlier_falls_back_to_yfinance(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    # KIS가 두 번 다 명백한 이상치(단위 오류 등)를 반환
    kis = _FakeKIS([{"price": 72500, "change_pct": 900.0}, {"price": 72500, "change_pct": 900.0}])

    def fake_fetch(symbol, max_daily_change=32.0):
        assert symbol == "000660.KS"
        return {"price": 210000, "prev_close": 205000, "change_pct": 2.44}

    monkeypatch.setattr("clients.market_data_client.fetch_kr_stock_realtime", fake_fetch)
    chg = alert_service._get_reversal_leader_change(kis, "000660", "SK하이닉스")
    assert chg == 2.44
    assert kis.calls == 2


def test_both_sources_fail_returns_none(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    kis = _FakeKIS([{}, {}])
    monkeypatch.setattr("clients.market_data_client.fetch_kr_stock_realtime", lambda symbol, max_daily_change=32.0: {})
    chg = alert_service._get_reversal_leader_change(kis, "005930", "삼성전자")
    assert chg is None
