"""node_send_telegram이 decision_guard와 실제로 연결돼 있는지 확인하는 배선 테스트.

로직 자체(어떤 상황에서 막는가)는 tests/test_decision_guard.py가 검증한다.
여기서는 "막힌 상태(gate.blocked=True)일 때 send_message가 절대 호출되지
않고, 대신 슬롯이 풀리고 경보가 나가는가"만 확인한다 — 가드를 만들어놓고
그래프에 연결을 안 하는 실수(연결 자체가 이번 사고의 핵심 대응이므로)를 잡는다.
"""
import graph.investment_graph as g


def _state(**overrides):
    base = {
        "ceo_report": "테스트 리포트 본문",
        "ceo_decisions": {},
        "raw_market_data": {},
        "kr_index_realtime": {},
        "errors": [],
        "date": "2026-07-22",
        "run_type": "pre_market",
    }
    base.update(overrides)
    return base


def test_blocked_gate_prevents_send_and_releases_slot(monkeypatch):
    calls = {"send_message": 0, "send_error_alert": 0, "released": None}

    monkeypatch.setattr(g, "send_message", lambda *a, **k: calls.__setitem__("send_message", calls["send_message"] + 1))
    monkeypatch.setattr(g, "send_error_alert", lambda *a, **k: calls.__setitem__("send_error_alert", calls["send_error_alert"] + 1))

    import services.decision_guard as dg
    import services.report_service as rs
    monkeypatch.setattr(dg, "gate_high_conviction_actions", lambda *a, **k: {
        "blocked": True, "reason": "테스트 사유", "actions": ["x"], "inconsistencies": ["y"],
    })
    monkeypatch.setattr(rs, "release_report_slot", lambda date, run_type: calls.__setitem__("released", (date, run_type)))

    result = g.node_send_telegram(_state())

    assert calls["send_message"] == 0, "gate가 막았는데 실제 발송이 나가면 안 됨"
    assert calls["send_error_alert"] == 1, "발송 보류 시 경보는 나가야 함"
    assert calls["released"] == ("2026-07-22", "pre_market")
    assert result is not None


def test_unblocked_gate_sends_normally(monkeypatch):
    calls = {"send_message": 0}
    monkeypatch.setattr(g, "send_message", lambda *a, **k: calls.__setitem__("send_message", calls["send_message"] + 1))

    import services.decision_guard as dg
    monkeypatch.setattr(dg, "gate_high_conviction_actions", lambda *a, **k: {
        "blocked": False, "reason": "", "actions": [], "inconsistencies": [],
    })

    g.node_send_telegram(_state())

    assert calls["send_message"] == 1
