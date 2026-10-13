from datetime import datetime
from zoneinfo import ZoneInfo

import services.post_deploy_check as pdc


class _R:
    def __init__(self, rows): self.rows = rows
    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None


def _patch_db(monkeypatch, pre=(), quality=(), branch=(), rep=True, forecast=True):
    def _rows(conn, date, like):
        if like == "pre_market": return list(pre)
        if like == "branch:briefing_quality": return list(quality)
        return list(branch)

    class _Conn:
        def execute(self, sql, *a, **k):
            if "forecast_log" in str(sql):
                return _R([("하락", 60.0, -3.0, -0.5, "independent")] if forecast else [])
            return _R([(1,)] if rep else [])
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(pdc, "_rows", _rows)
    monkeypatch.setattr(pdc, "get_conn", lambda: _Conn())


def test_all_good_has_no_actions(monkeypatch):
    _patch_db(monkeypatch, quality=[("branch:briefing_quality", "success", "빈리포트=0/15", "t")])
    body, actions = pdc.build_report("2026-10-14", None, {"mb": 330.0, "job": "pre_market", "at": "10-14 08:30"})
    assert actions == [] and "조치할 사항 없음" in body and "330MB" in body


def test_memory_danger_and_quality_fail_create_actions(monkeypatch):
    _patch_db(monkeypatch, quality=[("branch:briefing_quality", "fail", "상류 리포트 5/15개 비었거나 실패", "t")])
    body, actions = pdc.build_report("2026-10-14", None, {"mb": 470.0, "job": "pre_market", "at": "x"})
    assert len(actions) == 2 and "동시 실행 축소" in " ".join(actions)


def test_no_report_is_action(monkeypatch):
    _patch_db(monkeypatch, rep=False)
    body, actions = pdc.build_report("2026-10-14", None, {"mb": None})
    assert any("브리핑이 돌지 않았음" in a for a in actions)
    assert "측정 기록 없음" in body


def test_holiday_skip_not_flagged(monkeypatch):
    _patch_db(monkeypatch, pre=[("pre_market", "skipped", "KRX 비거래일", "t")], rep=False)
    _, actions = pdc.build_report("2026-10-14", None, {"mb": 300.0})
    assert not any("브리핑이 돌지 않았음" in a for a in actions)


def test_restart_after_briefing_noted(monkeypatch):
    _patch_db(monkeypatch)
    started = datetime(2026, 10, 14, 10, 5, tzinfo=ZoneInfo("Asia/Seoul"))
    body, _ = pdc.build_report("2026-10-14", started, {"mb": 300.0})
    assert "10:05에 시작" in body


def test_run_is_deduped_by_claim(monkeypatch):
    import services.report_service as rs
    monkeypatch.setattr(rs, "claim_report_slot", lambda d, t: False)
    assert pdc.run("2026-10-14") is False


def test_scheduler_registers_one_off_and_not_in_ledger():
    import scheduler as s
    from services.job_ledger import _EXPECTED_BY_WEEKDAY
    assert s.POST_DEPLOY_CHECK_DATE == "2026-10-14"
    assert callable(s.job_post_deploy_check)
    assert not any("post_deploy_check" in v for v in _EXPECTED_BY_WEEKDAY.values())


def test_job_is_actually_registered_for_the_date():
    import scheduler as s
    s.scheduler.remove_all_jobs()
    s.POST_DEPLOY_CHECK_DATE = "2099-01-01"      # 미래 날짜 → 등록돼야 함
    s.setup_jobs()
    assert s.scheduler.get_job("post_deploy_check") is not None
    s.scheduler.remove_all_jobs()
    s.POST_DEPLOY_CHECK_DATE = "2026-10-14"


def test_missing_independent_forecast_is_action(monkeypatch):
    _patch_db(monkeypatch, quality=[("branch:briefing_quality", "success", "ok", "t")], forecast=False)
    body, actions = pdc.build_report("2026-10-14", None, {"mb": 330.0, "job": "pre_market", "at": "x"})
    assert any("독립 예측가" in a for a in actions) and "예측 기록 없음" in body


def test_independent_forecast_shown_when_present(monkeypatch):
    _patch_db(monkeypatch, quality=[("branch:briefing_quality", "success", "ok", "t")])
    body, actions = pdc.build_report("2026-10-14", None, {"mb": 330.0, "job": "pre_market", "at": "x"})
    assert "독립 예측가: ✅ 하락 60%" in body and actions == []
