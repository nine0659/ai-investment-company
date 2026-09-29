"""보유 원장 신선도 — 실거래가 미래에셋이라 원장은 사람이 맞춰야 하고, 안 맞으면 조언 전체가 틀린다."""
from datetime import date, timedelta

from db.database import init_db
from services import profile_service


def _fresh_db():
    init_db()
    from sqlalchemy import text
    from db.database import get_conn
    with get_conn() as conn:
        conn.execute(text("DELETE FROM system_settings WHERE key=:k"),
                     {"k": profile_service._HOLDINGS_CONFIRMED_KEY})


def test_first_run_sets_baseline_silently():
    _fresh_db()
    today = date(2026, 9, 29)
    assert profile_service.get_holdings_stale_warning(today) == ""
    # 기준점이 기록됐으므로 34일 뒤까지는 조용
    assert profile_service.get_holdings_stale_warning(today + timedelta(days=34)) == ""


def test_warns_after_stale_threshold_and_confirm_resets():
    _fresh_db()
    today = date(2026, 9, 29)
    profile_service.get_holdings_stale_warning(today)          # 기준점
    later = today + timedelta(days=profile_service.HOLDINGS_STALE_DAYS + 1)
    msg = profile_service.get_holdings_stale_warning(later)
    assert "확인되지 않았습니다" in msg and "/holdings confirm" in msg
    assert profile_service.confirm_holdings() is True           # 오늘(실제 날짜) 기준 리셋
    assert profile_service.get_holdings_stale_warning() == ""


def test_confirm_subcommand_is_wired():
    import inspect
    from clients import telegram_bot
    assert '"confirm"' in inspect.getsource(telegram_bot._cmd_holdings)
