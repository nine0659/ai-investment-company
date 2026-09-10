"""node_save_report이 market_prediction_service.save_prediction을 더 이상
호출하지 않는지 회귀 테스트 (2026-09-10).

market_prediction_service.save_prediction()은 ceo_report 전문을 "상승/하락/
중립" 키워드로 재파싱해 market_predictions을 DELETE+INSERT했는데, 실제 CEO
출력형식은 그 단어들을 쓰지 않아(예: "시장 분위기: 좋음/나쁨/보통") 본문
아무 문장에서나 우연히 먼저 걸리는 값을 저장했다. 이게 ceo_agent.run()이
PRE/GLOBAL에서 이미 호출한 services.prediction_service.save_cio_prediction()
(macro_stance 기반 — 신뢰 가능한 구조화 데이터)이 쓴 행을 node_save_report
단계에서 매번 덮어써 지우고 있었다 — CEO의 "예측 적중률" 자기학습 루프가
매일 오염되던 버그. node_save_report에서 그 호출을 제거했다.
"""
import graph.investment_graph as ig
import services.market_prediction_service as market_prediction_service


def _make_state(**overrides):
    state = {
        "date": "2026-09-10", "run_type": "pre_market",
        "ceo_report": "테스트 브리핑 본문", "candidates": [], "sector_scores": [],
        "market_direction": "중립", "ceo_decisions": {}, "raw_market_data": {},
        "market_intelligence_report": "",
    }
    state.update(overrides)
    return state


def test_node_save_report_never_calls_market_prediction_service_save_prediction(monkeypatch):
    calls = []
    monkeypatch.setattr(
        market_prediction_service, "save_prediction",
        lambda *a, **k: calls.append((a, k)) or True,
    )
    import services.report_service as report_service
    monkeypatch.setattr(report_service, "save_report", lambda **k: None)

    ig.node_save_report(_make_state())

    assert calls == [], (
        "node_save_report이 market_prediction_service.save_prediction을 호출함 — "
        "prediction_service.save_cio_prediction()이 쓴 예측 행을 다시 덮어쓰는 회귀가 재발했다"
    )
