"""node_send_telegram이 심층 리포트 '요약'을 자동으로 후속 발송하는지 확인하는 배선 테스트.

2026-07-23: 그동안 /insight로 직접 조회해야만 보이던 심층 분석(매크로·글로벌서사·
이슈종목·수급·종목별 실측 기술지표)을 매 브리핑 뒤에 자동으로 붙여 보내기로 함 —
사용자가 명령어를 몰라서/까먹어서 못 보는 문제를 근본적으로 없앤다.

2026-09-03: 전문을 그대로 이어붙여 보내던 방식이 브리핑 1회당 5~7통으로 쪼개져
오는 플러딩을 유발한다는 사용자 피드백으로, node_deep_report가 미리 생성해 둔
3~5줄 요약(deep_report_summary)만 자동 발송하도록 변경. 전문은 여전히 DB에
저장되어 /insight로 조회 가능 — 요약이 비면(생성 실패 등) 전문으로 대체 발송하지
않고 조용히 생략한다(대체 발송하면 플러딩 문제로 되돌아가므로 금지).
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
        "deep_report_summary": "",
    }
    base.update(overrides)
    return base


def _unblocked_gate(monkeypatch):
    import services.decision_guard as dg
    monkeypatch.setattr(dg, "gate_high_conviction_actions", lambda *a, **k: {
        "blocked": False, "reason": "", "actions": [], "inconsistencies": [],
    })


def test_deep_report_summary_sent_as_followup_when_present(monkeypatch):
    _unblocked_gate(monkeypatch)
    sent = []
    monkeypatch.setattr(g, "send_message", lambda text: sent.append(text))

    g.node_send_telegram(_state(
        deep_report_content="매크로: 금리 급등 (전문 — 발송되면 안 됨)",
        deep_report_summary="매크로: 미 10Y 금리 급등, 위험회피 전환",
    ))

    assert len(sent) == 2, "메인 브리핑 + 심층 요약 두 통이 나가야 함"
    assert sent[0] == "메인 브리핑 본문"
    assert "미 10Y 금리 급등" in sent[1]
    assert "심층 분석" in sent[1]
    assert "/insight" in sent[1], "전문 조회 경로를 안내해야 함"
    assert "전문" not in sent[1] or "발송되면 안 됨" not in sent[1], \
        "요약이 아니라 전문 원문이 그대로 새어나가면 안 됨"


def test_no_second_message_when_summary_empty(monkeypatch):
    """전문이 있어도 요약 생성에 실패(빈 문자열)하면 전문으로 대체 발송하지 않는다 —
    대체 발송하면 5~7통 플러딩 문제로 되돌아간다."""
    _unblocked_gate(monkeypatch)
    sent = []
    monkeypatch.setattr(g, "send_message", lambda text: sent.append(text))

    g.node_send_telegram(_state(
        deep_report_content="매크로: 금리 급등 (전문)",
        deep_report_summary="",
    ))

    assert len(sent) == 1, "요약이 없으면 전문을 대신 보내지 말고 한 통만 나가야 함"


def test_no_second_message_when_deep_report_empty(monkeypatch):
    _unblocked_gate(monkeypatch)
    sent = []
    monkeypatch.setattr(g, "send_message", lambda text: sent.append(text))

    g.node_send_telegram(_state(deep_report_content="", deep_report_summary=""))

    assert len(sent) == 1, "심층 리포트가 없으면 한 통만 나가야 함"


def test_main_send_failure_skips_followup(monkeypatch):
    """메인 발송 자체가 실패하면 심층 요약도 보내지 않는다 (실패 상태에서 이어붙이지 않음)."""
    _unblocked_gate(monkeypatch)

    def _boom(text):
        if "메인" in text:
            raise RuntimeError("telegram down")

    calls = []
    monkeypatch.setattr(g, "send_message", lambda text: (calls.append(text), _boom(text)))

    g.node_send_telegram(_state(
        deep_report_content="매크로 내용",
        deep_report_summary="매크로 요약",
    ))

    assert calls == ["메인 브리핑 본문"], "메인 발송 실패 시 심층 요약을 이어 보내면 안 됨"
