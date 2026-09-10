"""agents/investment_committee.py 회귀 테스트.

2026-09-10 발견: _DIR_LABEL_RE가 "시장 방향성[：:\\s]+"만 허용해 프롬프트 자체가
지시하는 "[시장 방향성] 상승" 형식(대괄호로 헤더를 감쌈)과 매칭이 안 됐다.
매칭 실패 시 폴백이 전체 텍스트에서 _DIRECTIONS를 아무 위치에서나 찾는데,
출력 형식상 "[시장 방향 확률] 상승확률 XX%" 줄이 "[시장 방향성]" 줄보다 항상
먼저 나오고 그 안의 "상승확률"이 "상승"을 부분 문자열로 포함해서, 실제 결론이
"하락"이어도 market_direction이 거의 항상 "상승"으로 뒤집혀 저장됐다. 이 값은
ceo_agent 프롬프트·bull/bear 토론·portfolio_manager 컨텍스트·reports 테이블
(review_feedback_team의 예측 적중률 비교 기준)로 그대로 흘러들어간다.
"""
import agents.investment_committee as committee


def _report(direction_line: str, prob_line: str = "[시장 방향 확률] 상승확률 25% / 하락확률 75% — 근거") -> str:
    return (
        "[매크로 레짐] RISK-OFF — VIX 28\n"
        "[이벤트 리스크] HIGH — FOMC 임박\n"
        f"{prob_line}\n"
        f"{direction_line}\n"
        "[수급 팩트] 외국인 순매도 지속\n"
    )


def test_direction_extracted_from_bracket_header_not_flipped_by_probability_line(monkeypatch):
    """핵심 회귀: 상승확률 줄이 먼저 나와도 실제 방향(하락)을 정확히 뽑아야 한다."""
    report = _report("[시장 방향성] 하락")
    monkeypatch.setattr(committee, "chat", lambda system, context, max_tokens=2000: report)
    state = {"errors": []}
    result = committee.run(state)
    assert result["market_direction"] == "하락"


def test_direction_extracted_with_plain_colon_format(monkeypatch):
    report = _report("시장 방향성: 강한상승")
    monkeypatch.setattr(committee, "chat", lambda system, context, max_tokens=2000: report)
    state = {"errors": []}
    result = committee.run(state)
    assert result["market_direction"] == "강한상승"


def test_direction_defaults_to_neutral_when_label_absent(monkeypatch):
    report = "관련 라벨이 전혀 없는 텍스트입니다."
    monkeypatch.setattr(committee, "chat", lambda system, context, max_tokens=2000: report)
    state = {"errors": []}
    result = committee.run(state)
    assert result["market_direction"] == "중립"


def test_probability_normalized_to_100(monkeypatch):
    report = _report("[시장 방향성] 하락", prob_line="[시장 방향 확률] 상승확률 20% / 하락확률 70% — 근거")
    monkeypatch.setattr(committee, "chat", lambda system, context, max_tokens=2000: report)
    state = {"errors": []}
    result = committee.run(state)
    assert "상승확률 22%" in result["committee_report"]
    assert "하락확률 78%" in result["committee_report"]


def test_failure_returns_error_delta_not_mutated_list(monkeypatch):
    def _boom(system, context, max_tokens=2000):
        raise RuntimeError("openai down")
    monkeypatch.setattr(committee, "chat", _boom)

    original_errors = ["이전 오류"]
    state = {"errors": original_errors}
    result = committee.run(state)

    assert result["committee_report"] == "분석팀 보고 실패"
    assert result["errors"] == ["committee: openai down"]
    assert original_errors == ["이전 오류"], "원본 errors 리스트를 직접 오염시키면 안 됨"
