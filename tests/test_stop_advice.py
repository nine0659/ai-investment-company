"""손절 권고(services/stop_advice.py) 회귀 — 2026-09-30 현대차 -22% 사고 대응.

기존엔 ① 알림 문구가 판단을 사용자에게 떠넘겼고 ② check_portfolio_risk가 '하루 -7%'
급락에만 반응해 평단 대비 누적 -15% 초과를 못 잡았다.
"""
from services import alert_service
from services.stop_advice import build_advice, format_stop_alert, loss_tier


def test_no_advice_above_stop_line():
    assert build_advice(15, 446333, 400000) is None      # -10.4%
    assert loss_tier(-10.0) is None


def test_half_sell_by_default_and_math_by_code():
    a = build_advice(15, 446333, 347000)                  # 현대차 사고 재현
    assert a["action"] == "half"
    assert a["sell_qty"] == 8 and a["keep_qty"] == 7      # ceil(15/2)
    assert a["pnl_pct"] == -22.26
    assert a["proceeds"] == 8 * 347000
    assert a["realized_loss"] == round(8 * (347000 - 446333))
    assert a["further_10pct_loss"] == round(15 * 347000 * 0.10)


def test_thesis_broken_means_full_exit():
    a = build_advice(5, 446333, 347000, thesis_broken=True)
    assert a["action"] == "all" and a["sell_qty"] == 5 and a["keep_qty"] == 0


def test_single_share_sells_all():
    assert build_advice(1, 100, 80)["action"] == "all"


def test_invalid_inputs_return_none():
    assert build_advice(0, 100, 80) is None
    assert build_advice(5, 0, 80) is None
    assert build_advice(5, 100, 0) is None


def test_tier_is_deepest_crossed():
    assert loss_tier(-15.0) == -15.0
    assert loss_tier(-22.3) == -20.0
    assert loss_tier(-31.0) == -30.0


def test_message_leads_with_conclusion_and_action():
    a = build_advice(5, 446333, 347000)
    msg = format_stop_alert("현대차", "005380", 5, 446333, 347000, a)
    assert msg.index("결론") < msg.index("권고") < msg.index("근거")
    assert "3주 매도" in msg and "2주는 유지" in msg
    assert "자동 주문은 나가지 않습니다" in msg
    assert "원인 파악 후 대응 결정" not in msg           # 떠넘기는 옛 문구 금지


def test_portfolio_risk_sends_advice_on_slow_bleed(monkeypatch):
    """하루 등락 -1%여도 누적 -22%면 발송 — 서서히 빠진 종목을 놓치던 사각지대."""
    sent, marked = [], []
    monkeypatch.setattr(alert_service, "_send_telegram", lambda m: sent.append(m) or True)
    monkeypatch.setattr(alert_service, "_stop_tier_already_sent", lambda c, t: False)
    monkeypatch.setattr(alert_service, "_mark_sent", lambda d, c, t: marked.append((c, t)))
    h = {"code": "005380", "name": "현대차", "quantity": 5,
         "avg_price": 446333.0, "price": 347000, "change_pct": -1.0}
    alert_service._send_stop_advice("2026-09-30", h, (347000 - 446333) / 446333 * 100)
    assert len(sent) == 1 and "손절선 도달" in sent[0]
    assert marked == [("005380", "stop_advice_20")]


def test_portfolio_risk_dedupes_same_tier(monkeypatch):
    sent = []
    monkeypatch.setattr(alert_service, "_send_telegram", lambda m: sent.append(m) or True)
    monkeypatch.setattr(alert_service, "_stop_tier_already_sent", lambda c, t: True)
    h = {"code": "005380", "name": "현대차", "quantity": 5,
         "avg_price": 446333.0, "price": 347000, "change_pct": -1.0}
    alert_service._send_stop_advice("2026-09-30", h, -22.26)
    assert sent == []
