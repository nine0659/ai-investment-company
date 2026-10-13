from services import falsification_monitor as fm


def test_classify_real_world_vague_phrases():
    for t in ["수출 둔화", "성장성 불확실", "수요 회복 지연", "반도체 수요 둔화", ""]:
        assert fm.classify(t) == "vague", t


def test_classify_coded_and_manual():
    assert fm.classify("종가 85,000원 하회") == "coded"
    assert fm.classify("평단 대비 -15%") == "coded"
    assert fm.classify("KOSPI 7,000 이탈") == "coded"
    assert fm.classify("분기 영업이익 YoY -20% 미만") == "manual"


def test_stock_price_trigger_fires():
    r = fm.evaluate("종가 85,000원 하회 시 근거 훼손", price=84000, avg_price=90000, market={})
    assert len(r["fired"]) == 1
    r2 = fm.evaluate("종가 85,000원 하회", price=90000, avg_price=90000, market={})
    assert r2["fired"] == []


def test_vs_avg_trigger():
    assert fm.evaluate("평단 대비 -15% 손실", 80000, 100000, {})["fired"]
    assert not fm.evaluate("평단 대비 -15% 손실", 90000, 100000, {})["fired"]


def test_market_trigger_and_unchecked():
    mkt = {"usd_krw": {"close": 1460.0}, "vix": {"close": 18.0}}
    assert fm.evaluate("USD/KRW 1,450 상회", None, None, mkt)["fired"]
    assert not fm.evaluate("VIX 30 돌파", None, None, mkt)["fired"]
    r = fm.evaluate("KOSPI 7,000 이탈", None, None, mkt)   # kospi 값 없음 → 침묵 아닌 unchecked
    assert r["fired"] == [] and len(r["unchecked"]) == 1


def test_context_block(monkeypatch):
    monkeypatch.setattr(fm, "_load_holdings_with_falsification", lambda: [
        {"code": "000660", "name": "SK하이닉스", "avg_price": 100000, "falsification": "평단 대비 -15% 손실"},
        {"code": "005380", "name": "현대차", "avg_price": 200000, "falsification": "수출 둔화"},
    ])
    out = fm.build_status_context({"000660": 80000, "005380": 210000}, {})
    assert "🚨 SK하이닉스" in out and "측정 불가능한 반증 조건" in out and "현대차" in out


def test_prompt_demands_measurable_falsification_and_schema_unchanged():
    import agents.ceo_agent as ca
    assert "지표+기준값" in ca._CIO_CHARTER
    assert "모호한 문장 금지" in ca._CIO_CHARTER


def test_alert_dedup_and_fire(monkeypatch):
    import services.alert_service as al
    sent = []

    class _R:
        def __init__(self, v): self.v = v
        def fetchone(self): return self.v

    class _Conn:
        def execute(self, *a, **k): return _R(("평단 대비 -15% 손실",))
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(al, "get_conn", lambda: _Conn())
    monkeypatch.setattr(al, "_stop_tier_already_sent_type", lambda c, t, days: False)
    monkeypatch.setattr(al, "_send_telegram", lambda m: sent.append(m) or True)
    monkeypatch.setattr(al, "_mark_sent", lambda *a: None)
    al._send_falsification_alert("2026-10-13", {"code": "000660", "name": "SK하이닉스",
                                                  "price": 80000, "avg_price": 100000}, {})
    assert len(sent) == 1 and "반증 조건 발동" in sent[0]

    sent.clear()
    monkeypatch.setattr(al, "_stop_tier_already_sent_type", lambda c, t, days: True)
    al._send_falsification_alert("2026-10-13", {"code": "000660", "name": "SK하이닉스",
                                                  "price": 80000, "avg_price": 100000}, {})
    assert sent == []
