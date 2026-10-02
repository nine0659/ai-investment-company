"""services/recommendation_tracker_service.py의 조용한 스킵 사각지대 회귀 테스트.

2026-09-29 코드 감사에서 발견: daily_tracker(평일 16:20, 학습 루프의 핵심 잡 —
추천→추적→recommendation_tracking→귀인분석 재개 기준)가 DB 조회 실패나 추적
대상 0건일 때 경보 없이 조용히 스킵하도록 짜여 있었다. job_runs엔 "success"만
찍혀 daily_health가 못 잡는 사각지대 — 같은 날 오전에 고친 nav_service.record_nav()의
"보유 0개" 사각지대(2f3738b)와 동형 구조였다.
"""
import services.recommendation_tracker_service as tracker_service


def test_get_active_recommendations_alerts_on_db_failure(monkeypatch):
    def _raise_conn():
        raise RuntimeError("DB 연결 실패 (테스트)")

    monkeypatch.setattr(tracker_service, "get_conn", _raise_conn)

    alerts = []
    import clients.telegram_client as telegram_client
    monkeypatch.setattr(telegram_client, "send_error_alert", lambda msg: alerts.append(msg))

    result = tracker_service._get_active_recommendations()

    assert result == []
    assert len(alerts) == 1
    assert "추천 종목 조회 실패" in alerts[0]


def test_run_daily_tracker_alerts_when_no_active_recs(monkeypatch):
    monkeypatch.setattr(tracker_service, "_get_active_recommendations", lambda: [])

    alerts = []
    import clients.telegram_client as telegram_client
    monkeypatch.setattr(telegram_client, "send_error_alert", lambda msg: alerts.append(msg))

    result = tracker_service.run_daily_tracker()

    assert result == {"processed": 0, "target_hit": 0, "stop_hit": 0, "expired": 0}
    assert len(alerts) == 1
    assert "추적 대상 추천 종목이 0건" in alerts[0]


def test_run_daily_tracker_no_alert_when_recs_present(monkeypatch):
    # 정상 케이스(추적 대상이 있음)엔 이 경보가 오발동하면 안 된다.
    monkeypatch.setattr(tracker_service, "_get_active_recommendations", lambda: [
        {"id": 1, "date": "2026-09-01", "code": "005930", "name": "삼성전자",
         "entry_price": 70000, "stop_price": 60000, "target_price": 90000},
    ])
    monkeypatch.setattr(tracker_service, "_get_today_tracking", lambda today: {1})  # 이미 처리됨

    alerts = []
    import clients.telegram_client as telegram_client
    monkeypatch.setattr(telegram_client, "send_error_alert", lambda msg: alerts.append(msg))

    result = tracker_service.run_daily_tracker()

    assert result["processed"] == 0  # 이미 처리돼 skip
    assert alerts == []


# 2026-10-02: 목표가 알림이 추천 진입가 기준 +10.59%만 보여줘 실제 평단(-5.6%)을 가린 사고
def test_target_alert_shows_real_avg_loss_when_held():
    msg = tracker_service.format_target_alert(
        "SK하이닉스", "000660", "2026-08-30", 21, 1_653_000, 1_828_000, 10.59,
        "1,820,000원", 19, 1_935_965,
    )
    assert "내 수익률 -5.58%" in msg
    assert "1,935,965" in msg
    assert "손실 구간" in msg
    assert "실제 매매 아님" in msg


def test_target_alert_marks_virtual_when_not_held():
    msg = tracker_service.format_target_alert(
        "SK하이닉스", "000660", "2026-08-30", 21, 1_653_000, 1_828_000, 10.59,
        "1,820,000원", None, None,
    )
    assert "미보유·가상 추적" in msg
    assert "내 수익률" not in msg


def test_tracker_report_labels_virtual_and_held(monkeypatch):
    monkeypatch.setattr(tracker_service, "get_tracking_summary", lambda days=30: {
        "total": 1, "win_rate": 100.0, "avg_return": 10.59, "target_rate": 100.0,
        "items": [{"code": "000660", "name": "SK하이닉스", "rec_date": "2026-08-30",
                   "return_pct": 10.59, "days_held": 21, "status": "target_hit"}]})
    monkeypatch.setattr(tracker_service, "_get_held_codes", lambda: {"000660"})
    out = tracker_service.format_tracker_report()
    assert "가상 성과" in out
    assert "실보유" in out


def test_basis_line_held_uses_real_avg(monkeypatch):
    monkeypatch.setattr(tracker_service, "get_held_position", lambda code: (19, 1_935_965.0))
    out = tracker_service.basis_line("000660", 1_828_000, 1_653_000)
    assert "내 수익률 -5.58%" in out
    assert "실제 매매 아님" in out


def test_basis_line_not_held_marks_virtual(monkeypatch):
    monkeypatch.setattr(tracker_service, "get_held_position", lambda code: None)
    out = tracker_service.basis_line("000660", 1_828_000, 1_653_000)
    assert "미보유" in out and "가상" in out
    assert "내 수익률" not in out
