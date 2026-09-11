def test_execute_buy_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_KIS_TRADING", raising=False)

    from services.trading_service import TradingError, execute_buy

    try:
        execute_buy("005930", 1)
    except TradingError as e:
        assert "실주문 기능은 비활성화" in str(e)
    else:
        raise AssertionError("execute_buy should not reach KIS when trading is disabled")


def test_kis_place_order_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_KIS_TRADING", raising=False)

    from clients.kis_client import KISClient

    result = KISClient().place_order("005930", "buy", 1, 0)

    assert result["success"] is False
    assert result["mode"] == "disabled"


def test_auto_settings_force_off_when_trading_disabled(monkeypatch):
    monkeypatch.delenv("ENABLE_KIS_TRADING", raising=False)
    monkeypatch.setenv("AUTO_EXECUTE_BUY", "true")

    import config.settings as settings

    assert settings.ENABLE_KIS_TRADING is False
    assert settings.get_auto_setting("AUTO_EXECUTE_BUY") is False
