"""KIS 당일 등락률 stale 회귀 테스트 (2026-10-21).

09:00 동시호가 직후 KIS prdy_ctrt가 '어제 등락률'(-4.23%)을 그대로 줘서, 실제론
+6.62% 상승 중이던 SK하이닉스가 '급락 분석'으로 발송됐다. 등락률은 현재가/기준가로
코드가 직접 계산한다.
"""
import clients.kis_client as kis_client
from clients.kis_client import _safe_change_pct


def test_stale_prdy_ctrt_is_ignored_when_base_price_known():
    o = {"stck_sdpr": "1223000", "prdy_ctrt": "-4.23", "acml_vol": "803310"}
    assert _safe_change_pct(o, 1304000) == 6.62


def test_no_trade_yet_returns_zero_not_yesterday_change():
    o = {"stck_sdpr": "1223000", "prdy_ctrt": "-4.23", "acml_vol": "0"}
    assert _safe_change_pct(o, 1223000) == 0.0


def test_falls_back_to_kis_value_without_base_price():
    assert _safe_change_pct({"prdy_ctrt": "2.5"}, 1000) == 2.5


def test_garbage_values_do_not_raise():
    assert _safe_change_pct({"stck_sdpr": "abc", "prdy_ctrt": None}, 1000) == 0.0


def test_get_stock_price_uses_computed_change(monkeypatch):
    client = kis_client.KISClient()
    monkeypatch.setattr(client, "_headers", lambda tr: {})

    class R:
        def raise_for_status(self): pass
        def json(self):
            return {"output": {"stck_prpr": "1304000", "stck_sdpr": "1223000",
                               "prdy_ctrt": "-4.23", "acml_vol": "5000"}}

    monkeypatch.setattr(kis_client.requests, "get", lambda *a, **k: R())
    d = client.get_stock_price("000660", market="J")
    assert d["price"] == 1304000
    assert d["change_pct"] > 6
