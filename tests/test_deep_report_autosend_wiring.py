"""node_send_telegram이 심층 리포트를 자동으로 후속 발송하는지 확인하는 배선 테스트.

2026-07-23: 그동안 /insight로 직접 조회해야만 보이던 심층 분석(매크로·글로벌서사·
이슈종목·수급·종목별 실측 기술지표)을 매 브리핑 뒤에 자동으로 붙여 보내기로 함 —
사용자가 명령어를 몰라서/까먹어서 못 보는 문제를 근본적으로 없앤다.
"""
import graph.investment_graph as g


def _state(**overrides):
    base = {
        "ceo_report": "메인 브리핑 본문",
        "ceo_decisions": {},
        "raw_market_data": {},
        "kr_index_realtime": {},
        "errors": [],
        "date": "2026-07-23",
        "run_type": "pre_market",
        "deep_report_content": "",
    }
    base.update(overrides)
    return base


def _unblocked_gate(monkeypatch):
    import services.decision_guard as dg
    monkeypatch.setattr(dg, "gate_high_conviction_actions", lambda *a, **k: {
        "blocked": False, "reason": "", "actions": [], "inconsistencies": [],
    })


def test_deep_report_sent_as_followup_when_present(monkeypatch):
    _unblocked_gate(monkeypatch)
    sent = []
    monkeypatch.setattr(g, "send_message", lambda text: sent.append(text))

    g.node_send_telegram(_state(deep_report_content="매크로: 금리 급등"))

    assert len(sent) == 2, "메인 브리핑 + 심층 리포트 두 통이 나가야 함"
    assert sent[0] == "메인 브리핑 본문"
    assert "매크로: 금리 급등" in sent[1]
    assert "심층 분석" in sent[1]


def test_no_second_message_when_deep_report_empty(monkeypatch):
    _unblocked_gate(monkeypatch)
    sent = []
    monkeypatch.setattr(g, "send_message", lambda text: sent.append(text))

    g.node_send_telegram(_state(deep_report_content=""))

    assert len(sent) == 1, "심층 리포트가 없으면 한 통만 나가야 함"


def test_main_send_failure_skips_followup(monkeypatch):
    """메인 발송 자체가 실패하면 심층 리포트도 보내지 않는다 (실패 상태에서 이어붙이지 않음)."""
    _unblocked_gate(monkeypatch)

    def _boom(text):
        if "메인" in text:
            raise RuntimeError("telegram down")

    calls = []
    monkeypatch.setattr(g, "send_message", lambda text: (calls.append(text), _boom(text)))

    g.node_send_telegram(_state(deep_report_content="매크로 내용"))

    assert calls == ["메인 브리핑 본문"], "메인 발송 실패 시 심층 리포트를 이어 보내면 안 됨"
