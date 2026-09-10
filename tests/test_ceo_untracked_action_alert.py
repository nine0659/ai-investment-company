"""agents/ceo_agent.py 구조화 결정 로그 누락 감지 회귀 테스트 (2026-09-10).

2026-09-04 실제 운영 브리핑에서 CEO가 본문엔 "살 것: 삼성전기(009150) 3%"를
냈지만 =CIO_DECISION_START= 블록 자체를 통째로 빠뜨렸다. 그 결과
_parse_cio_decisions가 조용히 빈 dict를 반환해 승인 큐 발송·CIO 결정
아카이브·추천추적·리스크게이트가 전부 말없이 스킵됐다 — 사용자는 매수
문구를 읽었는데 그 어떤 안전장치도 작동하지 않았다. 본문에 실제 행동
지시가 있는데 구조화 블록이 없으면 경보로 드러내도록 고쳤다(자동 파싱
시도는 하지 않음 — 없는 값을 지어내지 않는다는 원칙 유지).
"""
import agents.ceo_agent as ceo_agent
from config.settings import RUN_TYPE_PRE

_MISSING_BLOCK_WITH_BUY = """━━━━━━━━━━━━━━━━━━━━━━━━━━
📡 장전 브리핑
━━━━━━━━━━━━━━━━━━━━━━━━━━

시장 분위기: 좋음 (기본 60%/우호 25%/비관 15%) — S&P500 선물 +0.91% 상승

오늘 할 일:
- 살 것: 삼성전기(009150) 3% — AI 서버 MLCC 수요 증가 기대
  파는 조건: AI 서버 수요 둔화 시
- 더 살 것: 없음
- 줄일 것·팔 것: 없음

주의: 쿼드러플위칭 임박으로 단기 변동성 확대 가능성

수치: KOSPI선물 [없음] · S&P500선물 [+0.91%] · 달러/원 [1,356원] · VIX [14.32]
━━━━━━━━━━━━━━━━━━━━━━━━━━"""

_MISSING_BLOCK_NO_ACTION = """시장 분위기: 보통 (기본 40%/우호 30%/비관 30%) — 혼조

오늘 할 일:
없음: 그대로 유지

주의: 특이사항 없음
"""

_WITH_BLOCK_AND_BUY = _MISSING_BLOCK_WITH_BUY + """

=CIO_DECISION_START=
stance|neutral|20
thesis|intact
analyst|agree|
new|009150|삼성전기|3|medium|mid|AI 서버 MLCC 수요 증가|developing|3.5:1|AI 서버 수요 둔화
=CIO_DECISION_END="""


def test_find_untracked_action_line_detects_missing_block_with_buy():
    result = ceo_agent._find_untracked_action_line(_MISSING_BLOCK_WITH_BUY)
    assert result is not None
    assert "삼성전기" in result


def test_find_untracked_action_line_none_when_block_present():
    result = ceo_agent._find_untracked_action_line(_WITH_BLOCK_AND_BUY)
    assert result is None


def test_find_untracked_action_line_none_when_no_actual_action():
    result = ceo_agent._find_untracked_action_line(_MISSING_BLOCK_NO_ACTION)
    assert result is None


def _make_state(**overrides):
    state = {
        "run_type": RUN_TYPE_PRE, "date": "2026-09-04", "errors": [],
        "raw_market_data": {}, "raw_kis_data": {}, "raw_news_data": {},
        "kr_index_realtime": {}, "consensus_data": {}, "dart_disclosures": [],
        "us_hot_stocks": [], "investment_thesis": "", "weekly_strategy_summary": "",
        "committee_report": "", "portfolio_report": "", "macro_report": "",
        "event_risk_report": "", "market_intelligence_report": "", "risk_report": "",
        "bull_case_report": "", "bear_case_report": "", "futures_report": "",
    }
    state.update(overrides)
    return state


def test_run_sends_alert_when_block_missing_but_buy_present(monkeypatch):
    monkeypatch.setattr(ceo_agent, "chat_ceo", lambda prompt, context, max_tokens=1800: _MISSING_BLOCK_WITH_BUY)

    alerts = []
    import clients.telegram_client as telegram_client
    monkeypatch.setattr(telegram_client, "send_error_alert", lambda msg: alerts.append(msg))

    import services.recommendation_service as recommendation_service
    monkeypatch.setattr(recommendation_service, "send_new_position_approvals", lambda date, recs: None)

    result = ceo_agent.run(_make_state())

    assert result["ceo_decisions"].get("new_positions") == [], "블록이 없으니 new_positions는 실제로 비어있어야 정상"
    assert any("삼성전기" in a and "구조화 결정 로그 누락" in a for a in alerts), (
        f"구조화 로그 누락 경보가 발송되지 않음: {alerts}"
    )


def test_run_does_not_alert_when_decision_block_present(monkeypatch):
    monkeypatch.setattr(ceo_agent, "chat_ceo", lambda prompt, context, max_tokens=1800: _WITH_BLOCK_AND_BUY)

    alerts = []
    import clients.telegram_client as telegram_client
    monkeypatch.setattr(telegram_client, "send_error_alert", lambda msg: alerts.append(msg))

    import services.recommendation_service as recommendation_service
    monkeypatch.setattr(recommendation_service, "send_new_position_approvals", lambda date, recs: None)

    result = ceo_agent.run(_make_state())

    assert len(result["ceo_decisions"].get("new_positions", [])) == 1
    assert alerts == []
