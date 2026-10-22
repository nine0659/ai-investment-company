"""스냅샷에 추정치·의견 시계열 컬럼을 쌓는 변경(2026-10-22)의 회귀 테스트 — 저장 내용과 기존 운영 테이블 마이그레이션.

이익 리비전(추정EPS 변화) 같은 신호는 '시계열'이 있어야 검증할 수 있는데 스냅샷이 추정치를 저장하지 않았다.
운영 DB에는 옛 구조의 테이블이 이미 있어 create_all이 컬럼을 더하지 못하므로 ALTER 마이그레이션이 필수다.
"""
import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text

import db.database as database
from db.database import get_conn, init_db
from services import universe_screener as us
from services import us_universe_screener as uus


@pytest.fixture
def clean():
    init_db()
    with get_conn() as c:
        c.execute(text("DELETE FROM universe_screen_daily"))
        c.execute(text("DELETE FROM us_screen_daily"))
    yield
    with get_conn() as c:
        c.execute(text("DELETE FROM universe_screen_daily"))
        c.execute(text("DELETE FROM us_screen_daily"))


def _kr_row():
    d = us.parse_integration({"totalInfos": [
        {"code": "eps", "value": "1,000원"}, {"code": "bps", "value": "5,000원"}, {"code": "cnsEps", "value": "1,400원"},
        {"code": "cnsPer", "value": "5.0배"}, {"code": "foreignRate", "value": "48.79%"},
        {"code": "highPriceOf52Weeks", "value": "20,000"}], "industryCode": "1",
        "consensusInfo": {"priceTargetMean": "15,000", "recommMean": "4.0"}, "dealTrendInfos": []})
    d["hist"] = {"ret_1m": 3.5, "ret_3m": -12.0, "ma50": 1.0, "ma200": 1.0}
    return {"code": "111111", "name": "A", "market": "KOSPI", "close": 10000.0, "chg_pct": 1.0, "volume": 1.0,
            "value_eok": 1.0, "mcap_eok": 5000.0, "detail": d, "signals": [{"key": "저평가", "text": ""}], "score": 1}


def test_kr_snapshot_stores_estimate_timeseries_fields(clean):
    us.save_daily({"scored": [_kr_row()]}, date="2026-10-22")
    with get_conn() as c:
        row = c.execute(text("SELECT cns_eps, recomm, bps, foreign_rate, ret_1m, ret_3m FROM universe_screen_daily")).fetchone()
    assert tuple(row) == (1400.0, 4.0, 5000.0, 48.79, 3.5, -12.0)


def test_kr_snapshot_handles_missing_history(clean):
    r = _kr_row()
    r["detail"]["hist"] = None
    us.save_daily({"scored": [r]}, date="2026-10-22")
    with get_conn() as c:
        row = c.execute(text("SELECT cns_eps, ret_1m, ret_3m FROM universe_screen_daily")).fetchone()
    assert row[0] == 1400.0 and row[1] is None and row[2] is None


def test_us_snapshot_stores_estimate_timeseries_fields(clean):
    row = {"code": "AAA", "name": "A", "mcap_usd": 5e9, "chg_pct": 1.0, "sector": "Tech", "industry": "Software"}
    d = uus.build_detail(row, {"eps": 5.0, "ni": 200.0, "equity": 1000.0, "revenue": 1300.0, "liabilities": 2000.0},
                         {"close": 100.0, "high52": 200.0, "ma50": 90.0, "ma200": 110.0, "ret_3m": -8.0})
    uus.apply_consensus(d, {"target": 150.0, "buy": 16, "hold": 3, "sell": 1, "analysts": 20, "fwd_eps": 10.0})
    rec = {**row, "detail": d, "signals": [], "warnings": [], "score": 0}
    uus.save_daily({"scored": [rec]}, date="2026-10-22")
    with get_conn() as c:
        got = c.execute(text("SELECT eps, fwd_eps, buy_ratio, analysts, ret_3m FROM us_screen_daily")).fetchone()
    assert tuple(got) == (5.0, 10.0, 0.8, 20.0, -8.0)


def test_us_prices_carry_three_month_return():
    import pandas as pd
    cl = pd.Series([100.0 + i for i in range(80)])
    # fetch_prices 내부 계산식과 같은 값이 build_detail로 전달되는지(키 존재)만 확인
    d = uus.build_detail({"industry": "x", "sector": "y"}, None, {"close": 179.0, "high52": 180.0, "ma50": 150.0,
                                                                   "ma200": None, "ret_3m": 5.0})
    assert d["ret_3m"] == 5.0
    assert (float(cl.iloc[-1]) / float(cl.iloc[-64]) - 1) * 100 == pytest.approx((179 / 116 - 1) * 100)


def _recreate_old_schema(table: str, old_cols_sql: str):
    with get_conn() as c:
        c.execute(text(f"DROP TABLE IF EXISTS {table}"))
        c.execute(text(f"CREATE TABLE {table} ({old_cols_sql})"))
    # SQLite 커넥션 풀이 옛 스키마를 캐시한 채 남아 있으면(앞선 그래프 테스트 등) 실제로 없는 컬럼에 'duplicate column'이 난다 — 풀을 비운다.
    database.engine.dispose()


def test_migration_adds_missing_columns_to_existing_old_tables():
    init_db()
    _recreate_old_schema("universe_screen_daily",
                         "id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, code TEXT NOT NULL, name TEXT, market TEXT, "
                         "close REAL, chg_pct REAL, mcap_eok REAL, per REAL, pbr REAL, cns_per REAL, eps REAL, target REAL, "
                         "high52 REAL, score INTEGER, signals TEXT, created_at TEXT")
    _recreate_old_schema("us_screen_daily",
                         "id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, code TEXT NOT NULL, name TEXT, sector TEXT, "
                         "close REAL, chg_pct REAL, mcap_usd REAL, fwd_pe REAL, roe REAL, target REAL, high52 REAL, score INTEGER, "
                         "signals TEXT, created_at TEXT")
    try:
        before = {c["name"] for c in sa_inspect(database.engine).get_columns("universe_screen_daily")}
        assert "cns_eps" not in before
        database._migrate_screen_tables()
        for table, cols in database._SCREEN_EXTRA_COLS.items():
            have = {c["name"] for c in sa_inspect(database.engine).get_columns(table)}
            assert {c for c, _ in cols} <= have, table
        database._migrate_screen_tables()                       # 두 번째 실행도 안전(이미 있으면 건너뜀)
    finally:
        with get_conn() as c:
            c.execute(text("DROP TABLE IF EXISTS universe_screen_daily"))
            c.execute(text("DROP TABLE IF EXISTS us_screen_daily"))
        database.engine.dispose()
        init_db()


def test_declared_columns_match_migration_list():
    """Table 정의의 추가 컬럼과 마이그레이션 목록이 어긋나면(한쪽만 수정) 운영에서 INSERT가 실패한다."""
    for table, cols in database._SCREEN_EXTRA_COLS.items():
        declared = {c.name for c in database.metadata.tables[table].columns}
        assert {c for c, _ in cols} <= declared, table


def test_migration_continues_past_a_column_that_already_exists(monkeypatch):
    """한 컬럼이 이미 있어 ALTER가 실패해도(다른 연결이 먼저 추가, 재시작 겹침 등) 나머지 컬럼은 계속 추가돼야 한다."""
    init_db()
    _recreate_old_schema("us_screen_daily",
                         "id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, code TEXT NOT NULL, name TEXT, sector TEXT, "
                         "close REAL, chg_pct REAL, mcap_usd REAL, fwd_pe REAL, roe REAL, target REAL, high52 REAL, score INTEGER, "
                         "signals TEXT, created_at TEXT, fwd_eps REAL")                   # fwd_eps만 이미 존재
    try:
        from sqlalchemy import inspect as real_inspect
        stale = real_inspect(database.engine)

        class Stale:                                                                        # 컬럼 목록이 실제보다 오래된 상황을 흉내
            def get_columns(self, table):
                return [c for c in stale.get_columns(table) if c["name"] != "fwd_eps"]
        import sqlalchemy
        monkeypatch.setattr(sqlalchemy, "inspect", lambda e: Stale())
        database._migrate_screen_tables()
        monkeypatch.undo()
        have = {c["name"] for c in real_inspect(database.engine).get_columns("us_screen_daily")}
        assert {"eps", "fwd_eps", "buy_ratio", "analysts", "ret_3m"} <= have
    finally:
        with get_conn() as c:
            c.execute(text("DROP TABLE IF EXISTS us_screen_daily"))
            c.execute(text("DROP TABLE IF EXISTS universe_screen_daily"))
        database.engine.dispose()
        init_db()
