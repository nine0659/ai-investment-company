"""agents/bull_bear_debate_team.py 회귀 테스트.

2026-09-03: TradingAgents(오픈소스 멀티에이전트 트레이딩 프레임워크) 리서치에서
확인한 Bull/Bear 토론 구조 적용. 기존엔 CEO 혼자 "독립 판단 원칙"으로 스스로에게
반론을 제기했으나, 같은 모델이 혼자 양쪽을 맡으면 실제 토론만큼의 긴장이 안 생긴다.
run_bear가 run_bull의 결과를 실제로 입력받아 반박하는지, 그리고 다른 순차 에이전트와
같은 errors 델타 규약을 지키는지가 이 테스트의 핵심.
"""
import agents.bull_bear_debate_team as bbd


def test_run_bull_sets_report(monkeypatch):
    monkeypatch.setattr(bbd, "chat", lambda system, context, max_tokens=500: "강세 논리 본문")
    state = {"committee_report": "", "portfolio_report": "", "market_direction": "상승", "errors": []}
    result = bbd.run_bull(state)
    assert result["bull_case_report"] == "강세 논리 본문"
    assert result["errors"] == []


def test_run_bear_receives_bull_case_in_context(monkeypatch):
    captured = {}

    def _fake_chat(system, context, max_tokens=500):
        captured["context"] = context
        return "약세 반박 본문"

    monkeypatch.setattr(bbd, "chat", _fake_chat)
    state = {
        "committee_report": "", "portfolio_report": "", "market_direction": "상승",
        "bull_case_report": "삼성전자 메모리 업사이클 진입", "errors": [],
    }
    result = bbd.run_bear(state)
    assert result["bear_case_report"] == "약세 반박 본문"
    assert "삼성전자 메모리 업사이클 진입" in captured["context"]


def test_run_bear_without_bull_case_still_works(monkeypatch):
    monkeypatch.setattr(bbd, "chat", lambda system, context, max_tokens=500: "약세 반박")
    state = {"committee_report": "", "portfolio_report": "", "market_direction": "중립", "errors": []}
    result = bbd.run_bear(state)
    assert result["bear_case_report"] == "약세 반박"


def test_run_bull_failure_returns_error_delta_not_mutated_list(monkeypatch):
    """다른 순차 에이전트와 같은 규약: state["errors"]를 직접 append하지 않고
    새 로컬 리스트를 델타로 교체 — 안 그러면 그래프 hop마다 2배씩 폭증한다."""
    def _boom(system, context, max_tokens=500):
        raise RuntimeError("openai down")
    monkeypatch.setattr(bbd, "chat", _boom)

    original_errors = ["이전 오류"]
    state = {"committee_report": "", "portfolio_report": "", "market_direction": "중립", "errors": original_errors}
    result = bbd.run_bull(state)

    assert result["bull_case_report"] == ""
    assert result["errors"] == ["bull_case: openai down"]
    assert original_errors == ["이전 오류"], "원본 errors 리스트를 직접 오염시키면 안 됨"


def test_run_bear_failure_returns_error_delta(monkeypatch):
    def _boom(system, context, max_tokens=500):
        raise RuntimeError("openai down")
    monkeypatch.setattr(bbd, "chat", _boom)

    state = {"committee_report": "", "portfolio_report": "", "market_direction": "중립",
             "bull_case_report": "", "errors": []}
    result = bbd.run_bear(state)

    assert result["bear_case_report"] == ""
    assert result["errors"] == ["bear_case: openai down"]
