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


# ── 2단계: 결과 점검 ────────────────────────────────────────────────────

def _seed(date, fc_dir="하락", prob=70.0, lo=-3.0, hi=-0.5, prev=-3.0, stance="중립"):
    from db.database import get_conn
    from sqlalchemy import text
    with get_conn() as c:
        c.execute(text("DELETE FROM forecast_log WHERE date=:d"), {"d": date})
        c.execute(text("DELETE FROM market_predictions WHERE date=:d"), {"d": date})
        c.execute(text("DELETE FROM job_runs WHERE date=:d"), {"d": date})
        c.execute(text("INSERT INTO forecast_log (date, run_type, direction, prob, low_pct, high_pct, prev_chg, reason, wrong_if) "
                       "VALUES (:d,'pre_market',:dir,:p,:lo,:hi,:pc,'r','w')"),
                  {"d": date, "dir": fc_dir, "p": prob, "lo": lo, "hi": hi, "pc": prev})
        c.execute(text("INSERT INTO market_predictions (date, run_type, predicted_dir) VALUES (:d,'pre_market',:s)"),
                  {"d": date, "s": stance})


def test_result_report_flags_neutral_after_shock_and_amplitude(monkeypatch):
    _seed("2031-03-05")
    closes = {"2031-03-04": 100.0, "2031-03-05": 94.0}     # 실제 -6%
    body, actions = pdc.build_result_report("2031-03-05", "2031-03-06", fetcher=lambda a, b: closes)
    assert "방향 적중" in body and "변동폭 과소평가" in body
    joined = " ".join(actions)
    assert "중립 스탠스" in joined and "1.5배" in joined


def test_result_report_flags_overconfident_miss_and_clean_case():
    _seed("2031-03-12", fc_dir="상승", prob=80.0, lo=0.5, hi=2.0, prev=0.5, stance="상승")
    closes = {"2031-03-11": 100.0, "2031-03-12": 98.0}
    body, actions = pdc.build_result_report("2031-03-12", "2031-03-13", fetcher=lambda a, b: closes)
    assert any("고확신" in a for a in actions)
    _seed("2031-03-19", fc_dir="하락", prob=60.0, lo=-2.0, hi=-0.5, prev=0.2, stance="하락")
    closes = {"2031-03-18": 100.0, "2031-03-19": 99.0}
    body, actions = pdc.build_result_report("2031-03-19", "2031-03-20", fetcher=lambda a, b: closes)
    assert actions == [] and "조치할 사항 없음" in body


def test_result_report_missing_forecast_is_action():
    from db.database import get_conn
    from sqlalchemy import text
    with get_conn() as c:
        c.execute(text("DELETE FROM forecast_log WHERE date='2031-04-02'"))
    body, actions = pdc.build_result_report("2031-04-02", "2031-04-03", fetcher=lambda a, b: {})
    assert any("독립 예측가" in a for a in actions)
