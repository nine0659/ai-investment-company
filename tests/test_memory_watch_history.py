"""메모리 최고치를 DB에 남기는지 — 재시작·OOM 뒤에도 '직전에 얼마였나'를 볼 수 있어야 한다(2026-10-28)."""
from db.database import init_db
from services import memory_watch as mw


def test_after_job_persists_daily_peak_and_keeps_max(monkeypatch):
    init_db()
    from db.database import get_conn
    from sqlalchemy import text
    with get_conn() as conn:
        conn.execute(text("DELETE FROM system_settings WHERE key='mem.peak.history'"))
    vals = iter([300.0, 300.0, 350.0, 340.0, 320.0, 320.0])   # (before, after) × 3 잡
    monkeypatch.setattr(mw, "container_rss_mb", lambda: next(vals))
    monkeypatch.setattr(mw, "trim", lambda: None)
    mw.after_job("job_a"); mw.after_job("job_b"); mw.after_job("job_c")
    h = mw.history()
    assert len(h) == 1
    only = next(iter(h.values()))
    assert only == {"mb": 350.0, "job": "job_b"}        # 하루 중 최고치만, 어떤 잡이었는지와 함께


def test_history_empty_when_nothing_recorded(monkeypatch):
    init_db()
    from db.database import get_conn
    from sqlalchemy import text
    with get_conn() as conn:
        conn.execute(text("DELETE FROM system_settings WHERE key='mem.peak.history'"))
    assert mw.history() == {}
