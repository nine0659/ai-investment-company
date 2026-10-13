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


# ── 2026-10-13: 서로 다른 증거 + 중립 판정자 ─────────────────────────

def test_bull_and_bear_see_different_evidence(monkeypatch):
    seen = {}
    monkeypatch.setattr(bbd, "chat", lambda system, context, max_tokens=500: (seen.setdefault(system[:6], context), "ok")[1])
    state = {"committee_report": "", "portfolio_report": "", "market_direction": "중립",
             "macro_report": "매크로-호재", "risk_report": "리스크-악재", "errors": []}
    bbd.run_bull(state)
    bbd.run_bear(state)
    bull_ctx = [v for k, v in seen.items() if k.startswith("당신은 강세")][0]
    bear_ctx = [v for k, v in seen.items() if k.startswith("당신은 약세")][0]
    assert "매크로-호재" in bull_ctx and "리스크-악재" not in bull_ctx
    assert "리스크-악재" in bear_ctx and "매크로-호재" not in bear_ctx


def test_judge_receives_both_sides_and_sets_verdict(monkeypatch):
    cap = {}
    monkeypatch.setattr(bbd, "chat_independent", lambda s, c, max_tokens=300: cap.update(ctx=c) or "우세: 약세")
    state = {"bull_case_report": "강세논리A", "bear_case_report": "약세반박B", "errors": []}
    r = bbd.run_judge(state)
    assert r["debate_verdict"] == "우세: 약세"
    assert "강세논리A" in cap["ctx"] and "약세반박B" in cap["ctx"]


def test_judge_skips_when_a_side_is_missing(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("LLM 호출되면 안 됨")
    monkeypatch.setattr(bbd, "chat_independent", _boom)
    r = bbd.run_judge({"bull_case_report": "x", "bear_case_report": "", "errors": []})
    assert r["debate_verdict"] == "" and r["errors"] == []


def test_judge_failure_returns_error_delta(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(bbd, "chat_independent", _boom)
    r = bbd.run_judge({"bull_case_report": "x", "bear_case_report": "y", "errors": ["old"]})
    assert r["errors"] == ["debate_judge: down"] and r["debate_verdict"] == ""


def test_judge_wired_between_bear_and_ceo():
    import inspect
    from graph import investment_graph as ig
    src = inspect.getsource(ig)
    assert '("bear_case",            "debate_judge")' in src
    assert '("debate_judge",         "ceo_agent")' in src
