"""드로다운 경보 재발송 억제 회귀 테스트.

2026-07-22 사고: job_daily_nav가 dedup 없이 매 거래일 동일 드로다운 경보를
재발송해 사용자가 "지속적으로 오류 알림"을 받는 문제가 확인됐다.
정책: 단계가 바뀔 때(none↔half↔all)는 즉시 발송, 같은 단계가 이어지면
_REALERT_DAYS(3일) 간격으로만 재발송한다.
"""
import uuid

from db.database import init_db
from services.nav_service import should_send_drawdown_alert


def _fresh_key(monkeypatch):
    """테스트마다 독립된 system_settings 키를 쓰도록 모듈 함수를 격리."""
    import services.nav_service as nav

    key = f"drawdown.last_alert.test.{uuid.uuid4().hex[:8]}"

    def fake_get_state():
        from db.database import get_conn
        from sqlalchemy import text
        with get_conn() as conn:
            row = conn.execute(
                text("SELECT value FROM system_settings WHERE key = :k"), {"k": key}
            ).fetchone()
        if row and row[0]:
            action, _, d = str(row[0]).partition("|")
            return action, d
        return "none", ""

    def fake_set_state(action, today):
        from db.database import get_conn
        from sqlalchemy import text
        with get_conn() as conn:
            conn.execute(
                text(
                    "INSERT INTO system_settings (key, value) VALUES (:k, :v) "
                    "ON CONFLICT (key) DO UPDATE SET value=:v, updated_at=CURRENT_TIMESTAMP"
                ),
                {"k": key, "v": f"{action}|{today}"},
            )

    monkeypatch.setattr(nav, "_drawdown_alert_state", fake_get_state)
    monkeypatch.setattr(nav, "_set_drawdown_alert_state", fake_set_state)


def test_first_alert_sends(monkeypatch):
    init_db()
    _fresh_key(monkeypatch)
    assert should_send_drawdown_alert("half", today="2026-07-20") is True


def test_same_level_next_day_suppressed(monkeypatch):
    init_db()
    _fresh_key(monkeypatch)
    assert should_send_drawdown_alert("half", today="2026-07-20") is True
    assert should_send_drawdown_alert("half", today="2026-07-21") is False
    assert should_send_drawdown_alert("half", today="2026-07-22") is False


def test_same_level_resends_after_interval(monkeypatch):
    init_db()
    _fresh_key(monkeypatch)
    assert should_send_drawdown_alert("half", today="2026-07-20") is True
    assert should_send_drawdown_alert("half", today="2026-07-23") is True  # 3일 경과


def test_escalation_sends_immediately(monkeypatch):
    init_db()
    _fresh_key(monkeypatch)
    assert should_send_drawdown_alert("half", today="2026-07-20") is True
    assert should_send_drawdown_alert("half", today="2026-07-21") is False
    assert should_send_drawdown_alert("all", today="2026-07-21") is True  # 단계 악화 — 즉시


def test_recovery_to_none_does_not_alert_but_resets(monkeypatch):
    init_db()
    _fresh_key(monkeypatch)
    assert should_send_drawdown_alert("half", today="2026-07-20") is True
    assert should_send_drawdown_alert("none", today="2026-07-21") is False
    # 정상화 이후 재악화되면 다시 즉시 발송 (억제 이력이 남아있지 않아야 함)
    assert should_send_drawdown_alert("half", today="2026-07-21") is True
