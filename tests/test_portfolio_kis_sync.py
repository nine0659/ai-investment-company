"""KIS 실계좌 동기화 — portfolio_positions가 실계좌와 어긋나도 아무도 몰랐던
문제(2026-08 발견) 재발 방지. sync_from_kis는 주문을 내지 않고 읽기 전용으로
DB를 실계좌 상태에 맞춘다.
"""
from db.database import init_db, get_conn
from sqlalchemy import text

from services.portfolio_service import sync_from_kis, get_portfolio


class _FakeKIS:
    def __init__(self, holdings):
        self._holdings = holdings

    def get_account_balance(self):
        return {"cash": 0, "total_eval": 0, "purchase_amt": 0, "holdings": self._holdings}


class _BrokenKIS:
    def get_account_balance(self):
        raise RuntimeError("네트워크 오류")


def setup_function(_):
    init_db()
    with get_conn() as conn:
        conn.execute(text("DELETE FROM portfolio_positions"))


def test_new_holding_registered():
    kis = _FakeKIS([{"code": "005930", "name": "삼성전자", "qty": 10, "avg_price": 70000}])
    changes = sync_from_kis(kis)
    assert changes["new"] == ["005930"]
    pf = get_portfolio()
    assert len(pf) == 1
    assert pf[0]["quantity"] == 10


def test_quantity_drift_corrected():
    with get_conn() as conn:
        conn.execute(
            text(
                "INSERT INTO portfolio_positions "
                "(code, name, quantity, avg_price, entry_date, status) "
                "VALUES ('005930', '삼성전자', 5, 65000, '2026-07-01', 'holding')"
            )
        )
    # 실계좌는 15주 — 시스템 밖에서 추가매수가 있었던 상황
    kis = _FakeKIS([{"code": "005930", "name": "삼성전자", "qty": 15, "avg_price": 68000}])
    changes = sync_from_kis(kis)
    assert changes["updated"] == ["005930"]
    pf = get_portfolio()
    assert pf[0]["quantity"] == 15


def test_position_missing_from_kis_is_closed_not_deleted():
    with get_conn() as conn:
        conn.execute(
            text(
                "INSERT INTO portfolio_positions "
                "(code, name, quantity, avg_price, entry_date, status) "
                "VALUES ('000660', 'SK하이닉스', 10, 2000000, '2026-07-01', 'holding')"
            )
        )
    # 실계좌엔 이제 없음 — 시스템 밖(MTS 등)에서 매도된 상황
    changes = sync_from_kis(_FakeKIS([]))
    assert changes["closed"] == ["000660"]
    # 삭제가 아니라 status만 변경 — 이력 보존
    with get_conn() as conn:
        row = conn.execute(
            text("SELECT status FROM portfolio_positions WHERE code='000660'")
        ).fetchone()
    assert row[0] == "sold"
    assert get_portfolio() == []  # holding 목록에서는 빠짐


def test_draft_rows_untouched():
    with get_conn() as conn:
        conn.execute(
            text(
                "INSERT INTO portfolio_positions "
                "(code, name, quantity, avg_price, entry_date, status) "
                "VALUES ('005930', '삼성전자', 0, 0, '2026-07-01', 'draft')"
            )
        )
    sync_from_kis(_FakeKIS([]))
    with get_conn() as conn:
        row = conn.execute(
            text("SELECT status FROM portfolio_positions WHERE code='005930'")
        ).fetchone()
    assert row[0] == "draft"  # draft는 실계좌 유무와 무관하게 그대로


def test_kis_failure_does_not_crash_or_wipe_data():
    with get_conn() as conn:
        conn.execute(
            text(
                "INSERT INTO portfolio_positions "
                "(code, name, quantity, avg_price, entry_date, status) "
                "VALUES ('005930', '삼성전자', 10, 70000, '2026-07-01', 'holding')"
            )
        )
    changes = sync_from_kis(_BrokenKIS())
    assert changes == {"new": [], "updated": [], "closed": []}
    assert len(get_portfolio()) == 1  # 기존 데이터 그대로 보존
