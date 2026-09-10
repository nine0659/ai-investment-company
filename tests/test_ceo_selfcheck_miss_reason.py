"""ceo_agent.py 장마감 자기점검 '오판 원인' 추출 회귀 테스트 (2026-09-10).

services/prediction_service.get_selfcheck_context()는 LLM에게 "적중·불일치·
오판 원인 반드시 명시"라고 지시로만 주입했는데, ceo_agent._build_prompt_close()의
실제 출력 템플릿엔 그 라벨을 쓰라는 칸이 없어서 ceo_agent.run()의
`r"오판 원인[:\\s:\\uff1a]*([^\\n]{5,80})"` 추출이 사실상 항상 빈 문자열이었다
(DB miss_reason 컬럼엔 계속 저장됐지만 아무도 읽지 않아 안 걸렸다). 출력
템플릿에 "자기점검: ... 오판 원인: ..." 줄을 명시적으로 추가해 고쳤다 — 이
테스트는 (1) 프롬프트 템플릿에 그 라벨이 실제로 지시돼 있는지, (2) LLM이 그
형식을 따랐을 때 추출이 실제로 동작하는지를 검증한다.
"""
import agents.ceo_agent as ceo_agent
from config.settings import RUN_TYPE_CLOSE


def test_close_prompt_instructs_miss_reason_label():
    prompt = ceo_agent._build_prompt_close()
    assert "오판 원인" in prompt, (
        "장마감 프롬프트 템플릿에 '오판 원인' 라벨 지시가 없음 — "
        "run()의 추출 정규식이 항상 빈 값을 낼 것"
    )


def _make_state(**overrides):
    state = {
        "run_type": RUN_TYPE_CLOSE, "date": "2026-09-10", "errors": [],
        "raw_market_data": {}, "raw_kis_data": {}, "raw_news_data": {},
        "kr_index_realtime": {"kospi": {"current": 3000.0, "change_pct": -1.2}},
        "consensus_data": {}, "dart_disclosures": [], "us_hot_stocks": [],
        "investment_thesis": "", "weekly_strategy_summary": "",
        "committee_report": "", "portfolio_report": "", "macro_report": "",
        "event_risk_report": "", "market_intelligence_report": "", "risk_report": "",
        "bull_case_report": "", "bear_case_report": "", "korea_spot_report": "",
        "sector_report": "", "money_flow_report": "", "issue_stocks_report": "",
        "news_report": "", "bigfigure_report": "", "midterm_stock_report": "",
        "review_report": "",
    }
    state.update(overrides)
    return state


def test_miss_reason_extracted_when_llm_follows_new_template(monkeypatch):
    """LLM이 새 템플릿대로 '오판 원인:' 라벨을 쓰면 update_actual_result가
    실제 추출된 사유를 받는지 검증 — 예전엔 라벨 자체가 안 쓰여 항상 ""였다."""
    fake_report = (
        "오늘 시장: KOSPI -1.2% · 외국인 팔았다 500억 · 기관 샀다 200억\n\n"
        "자기점검: 불일치 — 오판 원인: 반도체 급락 과소평가\n\n"
        "오늘 할 일:\n없음\n"
    )
    monkeypatch.setattr(ceo_agent, "chat_ceo", lambda prompt, context, max_tokens=1800: fake_report)

    captured = {}

    def _fake_update(date, actual_kospi, miss_reason=""):
        captured["date"] = date
        captured["actual_kospi"] = actual_kospi
        captured["miss_reason"] = miss_reason

    import services.prediction_service as prediction_service
    monkeypatch.setattr(prediction_service, "update_actual_result", _fake_update)
    monkeypatch.setattr(prediction_service, "get_selfcheck_context", lambda date: "")
    monkeypatch.setattr(prediction_service, "get_accuracy_summary", lambda days=20: "")

    result = ceo_agent.run(_make_state())

    assert "브리핑 생성 실패" not in result["ceo_report"]
    assert captured.get("miss_reason") == "반도체 급락 과소평가", (
        f"오판 원인 추출 실패 — captured={captured}"
    )
    assert captured.get("actual_kospi") == -1.2


def test_miss_reason_empty_when_llm_reports_a_hit(monkeypatch):
    """적중 시에는 '해당없음'으로 명시하도록 템플릿이 지시 — 추출값이 그 문구를
    그대로 담아도(빈 문자열이 아니어도) 정상."""
    fake_report = (
        "오늘 시장: KOSPI +0.8% · 외국인 샀다 300억 · 기관 팔았다 100억\n\n"
        "자기점검: 적중 — 오판 원인: 해당없음\n\n"
        "오늘 할 일:\n없음\n"
    )
    monkeypatch.setattr(ceo_agent, "chat_ceo", lambda prompt, context, max_tokens=1800: fake_report)

    captured = {}

    def _fake_update(date, actual_kospi, miss_reason=""):
        captured["miss_reason"] = miss_reason

    import services.prediction_service as prediction_service
    monkeypatch.setattr(prediction_service, "update_actual_result", _fake_update)
    monkeypatch.setattr(prediction_service, "get_selfcheck_context", lambda date: "")
    monkeypatch.setattr(prediction_service, "get_accuracy_summary", lambda days=20: "")

    result = ceo_agent.run(_make_state(**{"kr_index_realtime": {"kospi": {"current": 3000.0, "change_pct": 0.8}}}))

    assert "브리핑 생성 실패" not in result["ceo_report"]
    assert captured.get("miss_reason") == "해당없음"
