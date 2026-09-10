"""agents/emergency_monitor_agent.py._check_thesis_invalidation 회귀 테스트 (2026-09-10).

두 버그를 같은 함수에서 발견·수정했다(둘 다 이 검사가 2026-09-10 전까지는
thesis.invalidation이 항상 빈 값이라 이 지점까지 도달한 적이 없어 안 걸렸다
— thesis_agent.py 파싱 버그 수정으로 이제 실제로 매 정시 호출된다):
1) YES 판정은 result.strip()으로 하면서 상세 설명 슬라이싱(result[4:])은
   원본(비trim) result에 해왔다 — LLM 응답 앞에 공백/개행이 섞이면 상세
   설명 앞부분이 깨진 채("S: 금리 인상..." 처럼) 알림에 노출된다.
2) market_data에서 change_pct가 없으면 "N/A" 문자열이 :+.2f 숫자 포맷을
   만나 ValueError로 크래시했다 — 데이터 수집 실패 한 번에 그 시간대
   검사 전체가(try/except로 조용히) 스킵됐다.
"""
import agents.emergency_monitor_agent as emergency_monitor_agent

_FULL_MARKET_DATA = {
    "kospi":   {"close": 3050.0, "change_pct": -1.2},
    "vix":     {"close": 22.0},
    "usd_krw": {"close": 1380.0},
    "us10y":   {"close": 4.3},
}


def _patch_common(monkeypatch, chat_response):
    import services.thesis_service as thesis_service
    import services.alert_service as alert_service
    import clients.openai_client as openai_client

    monkeypatch.setattr(thesis_service, "get_active_thesis",
                         lambda: {"invalidation": "미국 정책금리 추가 인상"})
    monkeypatch.setattr(alert_service, "_already_sent", lambda *a, **k: False)
    monkeypatch.setattr(alert_service, "TYPE_RISK", "risk")

    captured = {}
    monkeypatch.setattr(alert_service, "_mark_sent", lambda *a, **k: None)

    def _fake_send_alert(type_, title, message, **k):
        captured["message"] = message
    monkeypatch.setattr(alert_service, "send_alert", _fake_send_alert)
    monkeypatch.setattr(openai_client, "chat", lambda *a, **k: chat_response)
    return captured


def test_detail_extraction_strips_leading_whitespace_correctly(monkeypatch):
    """LLM 응답 앞에 개행/공백이 섞여도 상세 설명이 깨지지 않아야 한다."""
    captured = _patch_common(monkeypatch, "\n  YES: 미국 금리 추가 인상 확정")
    emergency_monitor_agent._check_thesis_invalidation(_FULL_MARKET_DATA, {})
    assert "미국 금리 추가 인상 확정" in captured["message"]
    assert "S: 미국" not in captured["message"], "슬라이싱이 원본(비trim) 문자열 기준이라 앞글자가 깨짐"


def test_detail_extraction_without_leading_whitespace_still_works(monkeypatch):
    captured = _patch_common(monkeypatch, "YES: 미국 금리 추가 인상 확정")
    emergency_monitor_agent._check_thesis_invalidation(_FULL_MARKET_DATA, {})
    assert "미국 금리 추가 인상 확정" in captured["message"]


def test_no_alert_when_llm_says_no(monkeypatch):
    import services.alert_service as alert_service
    called = []
    _patch_common(monkeypatch, "NO")
    monkeypatch.setattr(alert_service, "send_alert", lambda *a, **k: called.append(1))
    emergency_monitor_agent._check_thesis_invalidation(_FULL_MARKET_DATA, {})
    assert called == []


def test_missing_kospi_change_pct_does_not_crash(monkeypatch):
    """market_data 수집 실패(kospi 데이터 없음)로도 ValueError 없이 정상 동작해야 한다."""
    captured = _patch_common(monkeypatch, "YES: 지정학 리스크 확대")
    emergency_monitor_agent._check_thesis_invalidation({}, {})  # kospi/vix 등 전부 없음
    assert "지정학 리스크 확대" in captured["message"]
