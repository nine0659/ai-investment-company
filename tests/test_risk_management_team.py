"""agents/risk_management_team.py 회귀 테스트.

2026-09-10 발견: _RISK_LEVEL_RE가 "종합 리스크 레벨[：:\\s]*"만 허용해, 같은
프롬프트가 예시로 보여주는 "종합 리스크 레벨(높음·중간·낮음)" 괄호 형식을 LLM이
그대로 따르면 매칭에 실패하고 항상 기본값 "중간"으로 조용히 떨어졌다 — "높음"
리스크가 감지돼도 state["risk_level"]에는 절대 반영되지 않는 버그.
"""
import agents.risk_management_team as risk_team


def test_risk_level_extracted_from_paren_format(monkeypatch):
    report = "종합 리스크 레벨(높음)\n1. 금리 급등\n2. 환율 변동성 확대\n"
    monkeypatch.setattr(risk_team, "chat", lambda system, context, max_tokens=2000: report)
    state = {"errors": []}
    result = risk_team.run(state)
    assert result["risk_level"] == "높음"


def test_risk_level_extracted_with_plain_colon_format(monkeypatch):
    report = "종합 리스크 레벨: 낮음\n1. 특이사항 없음\n"
    monkeypatch.setattr(risk_team, "chat", lambda system, context, max_tokens=2000: report)
    state = {"errors": []}
    result = risk_team.run(state)
    assert result["risk_level"] == "낮음"


def test_risk_level_defaults_to_medium_when_label_absent(monkeypatch):
    report = "라벨 없는 텍스트"
    monkeypatch.setattr(risk_team, "chat", lambda system, context, max_tokens=2000: report)
    state = {"errors": []}
    result = risk_team.run(state)
    assert result["risk_level"] == "중간"


def test_risks_extracted_from_numbered_list(monkeypatch):
    report = "종합 리스크 레벨(중간)\n1. 금리 리스크\n2. 환율 리스크\n3. 지정학 리스크\n요약 문장.\n"
    monkeypatch.setattr(risk_team, "chat", lambda system, context, max_tokens=2000: report)
    state = {"errors": []}
    result = risk_team.run(state)
    assert result["risks"] == ["1. 금리 리스크", "2. 환율 리스크", "3. 지정학 리스크"]


def test_failure_returns_error_delta_not_mutated_list(monkeypatch):
    def _boom(system, context, max_tokens=2000):
        raise RuntimeError("openai down")
    monkeypatch.setattr(risk_team, "chat", _boom)

    original_errors = ["이전 오류"]
    state = {"errors": original_errors}
    result = risk_team.run(state)

    assert result["risk_report"] == "분석 실패"
    assert result["errors"] == ["risk_team: openai down"]
    assert original_errors == ["이전 오류"], "원본 errors 리스트를 직접 오염시키면 안 됨"
