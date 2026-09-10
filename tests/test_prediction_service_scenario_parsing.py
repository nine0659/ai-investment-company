"""services/prediction_service.py 시나리오 확률(_parse_scenarios) 회귀 테스트
(2026-09-10).

CEO 출력 템플릿엔 원래 "꼬리위험"이라는 문구가 없어 baseline/bull/bear/
tail_risk가 항상 비어있었다. "시장 분위기" 줄에 "(기본 X%/우호 Y%/비관 Z%)"를
병기하도록 agents/ceo_agent.py의 PRE/GLOBAL 프롬프트를 확장했고,
tail_risk는 이미 있는 "주의:" 줄을 폴백으로 파싱하도록 고쳤다(프롬프트 변경
없이 파서만 수정).
"""
import agents.ceo_agent as ceo_agent
import services.prediction_service as prediction_service


def test_pre_and_global_prompts_request_scenario_probabilities():
    for builder in (ceo_agent._build_prompt_pre, ceo_agent._build_prompt_global):
        prompt = builder()
        assert "기본" in prompt and "우호" in prompt and "비관" in prompt, (
            f"{builder.__name__}에 시나리오 확률 지시가 빠져있음"
        )


def test_close_prompt_does_not_request_scenario_probabilities():
    """CLOSE는 사후 브리핑이라 시나리오 확률이 필요 없다 — 괜히 추가하지 않았는지 확인."""
    prompt = ceo_agent._build_prompt_close()
    assert "기본" not in prompt or "우호" not in prompt or "비관" not in prompt


def test_parse_scenarios_extracts_from_market_mood_line():
    report = "시장 분위기: 좋음 (기본 55%/우호 30%/비관 15%) — 반도체 수급 개선\n\n오늘 할 일:\n없음\n"
    baseline, bull, bear, tail = prediction_service._parse_scenarios(report)
    assert baseline == 55.0
    assert bull == 30.0
    assert bear == 15.0


def test_parse_scenarios_tail_risk_falls_back_to_attention_line():
    report = (
        "시장 분위기: 보통 (기본 50%/우호 25%/비관 25%) — 혼조\n\n"
        "오늘 할 일:\n없음\n\n"
        "주의: 미국 CPI 발표 시 변동성 확대 가능\n"
    )
    _, _, _, tail = prediction_service._parse_scenarios(report)
    assert "미국 CPI 발표" in tail


def test_parse_scenarios_prefers_explicit_tail_risk_label_when_present():
    report = "꼬리위험: 지정학 리스크 급변\n주의: 다른 내용\n"
    _, _, _, tail = prediction_service._parse_scenarios(report)
    assert "지정학 리스크 급변" in tail
