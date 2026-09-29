"""KISClient.get_stock_price_with_fallback 회귀 테스트 (2026-09-29 추가).

전수 코드감사에서 발견: KIS 조회 메서드 8개 중 yfinance 폴백이 있는 게 하나도
없어(기존 clients/market_data_client.py의 KIS→yfinance 폴백 패턴은 2026-09-21
반전분석 수정 단 한 곳에만 적용됨), KIS 장애 시 포트폴리오 평가금액·시장감시
알림이 그대로 죽었다. "가격류"(현재가·등락률)만 범위를 잡아 폴백을 추가한다 —
등락률순위 등 KIS 전용 데이터는 yfinance에 대체 소스가 없어 대상이 아니다.
"""
import clients.kis_client as kis_client


def _make_client():
    client = kis_client.KISClient()
    return client


def test_falls_back_to_yfinance_when_kis_empty(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(client, "get_stock_price", lambda code, market=None: {})

    import clients.market_data_client as market_data_client
    monkeypatch.setattr(
        market_data_client, "fetch_kr_stock_realtime",
        lambda symbol: {"price": 71000, "change_pct": 1.23} if symbol.endswith(".KS") else {},
    )

    result = client.get_stock_price_with_fallback("005930")

    assert result["price"] == 71000
    assert result["change_pct"] == 1.23
    assert result["price_source"] == "yfinance_fallback"


def test_no_fallback_when_kis_succeeds(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(
        client, "get_stock_price",
        lambda code, market=None: {"price": 70000, "per": 12.3},
    )

    import clients.market_data_client as market_data_client
    called = []
    monkeypatch.setattr(
        market_data_client, "fetch_kr_stock_realtime",
        lambda symbol: called.append(symbol) or {"price": 99999},
    )

    result = client.get_stock_price_with_fallback("005930")

    assert result["price"] == 70000
    assert "price_source" not in result
    assert called == []  # KIS가 성공했으면 yfinance는 호출조차 안 돼야 함


def test_returns_empty_when_both_sources_fail(monkeypatch):
    client = _make_client()
    monkeypatch.setattr(client, "get_stock_price", lambda code, market=None: {})

    import clients.market_data_client as market_data_client
    monkeypatch.setattr(market_data_client, "fetch_kr_stock_realtime", lambda symbol: {})

    result = client.get_stock_price_with_fallback("005930")

    assert not result.get("price")
