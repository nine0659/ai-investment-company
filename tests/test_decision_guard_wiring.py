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
    import services.job_ledger as jl
    recorded = []
    monkeypatch.setattr(jl, "record_job", lambda n, st, d="": recorded.append((n, st, d)))

    # 보류 전 재수집은 실제 네트워크를 타면 안 된다 — 재수집 실패(보류 유지) 경로로 고정
    monkeypatch.setattr(dg, "recheck_blocked_gate", lambda *a, **k: {"blocked": True, "rechecked": False})

    result = g.node_send_telegram(_state())

    assert calls["send_message"] == 0, "gate가 막았는데 실제 발송이 나가면 안 됨"
    assert calls["send_error_alert"] == 1, "발송 보류 시 경보는 나가야 함"
    assert calls["released"] == ("2026-07-22", "pre_market")
    # 2026-10-28: 보류가 경보 1통뿐이라 브리핑 미발송을 DB에서 확인할 수 없었다 → 대장에 fail로 남겨 헬스체크가 잡는다
    assert [(n, st) for n, st, _ in recorded if n == "branch:send_gate"] == [("branch:send_gate", "fail")]
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


def _blocked_env(monkeypatch, recheck_result):
    calls = {"send_message": 0, "released": None}
    monkeypatch.setattr(g, "send_message", lambda *a, **k: calls.__setitem__("send_message", calls["send_message"] + 1))
    monkeypatch.setattr(g, "send_error_alert", lambda *a, **k: None)
    import services.decision_guard as dg
    import services.report_service as rs
    import services.job_ledger as jl
    monkeypatch.setattr(dg, "gate_high_conviction_actions", lambda *a, **k: {
        "blocked": True, "reason": "최초 사유", "actions": ["x"], "inconsistencies": ["y"]})
    monkeypatch.setattr(dg, "recheck_blocked_gate", lambda *a, **k: recheck_result)
    monkeypatch.setattr(rs, "release_report_slot", lambda d, r: calls.__setitem__("released", (d, r)))
    recorded = []
    monkeypatch.setattr(jl, "record_job", lambda n, st, d="": recorded.append((n, st, d)))
    return calls, recorded


def test_recheck_clears_transient_inconsistency_and_sends(monkeypatch):
    calls, recorded = _blocked_env(monkeypatch, {"blocked": False, "reason": "", "actions": ["x"],
                                                 "inconsistencies": [], "rechecked": True})
    g.node_send_telegram(_state())
    assert calls["send_message"] >= 1 and calls["released"] is None
    assert [(n, st) for n, st, _ in recorded if n == "branch:send_gate"] == [("branch:send_gate", "success")]


def test_recheck_still_inconsistent_keeps_hold(monkeypatch):
    calls, recorded = _blocked_env(monkeypatch, {"blocked": True, "reason": "재수집 후에도", "actions": ["x"],
                                                 "inconsistencies": ["y"], "rechecked": True})
    g.node_send_telegram(_state())
    assert calls["send_message"] == 0 and calls["released"] == ("2026-07-22", "pre_market")
    assert [(n, st) for n, st, d in recorded if n == "branch:send_gate"] == [("branch:send_gate", "fail")]
    assert "재수집 후에도" in [d for n, _, d in recorded if n == "branch:send_gate"][0]
