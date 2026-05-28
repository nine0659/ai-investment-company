"""
db/database.py — 중앙 데이터베이스 레이어

DATABASE_URL 환경변수:
  설정 시  → Supabase PostgreSQL (운영)
  미설정 시 → SQLite 로컬 (개발)

사용법:
  from db.database import get_conn
  from sqlalchemy import text

  with get_conn() as conn:
      rows = conn.execute(text("SELECT ..."), {"param": value}).fetchall()
"""
import contextlib
import logging
import os
from pathlib import Path

from sqlalchemy import (
    Column, Float, Integer, MetaData, String, Table, Text,
    create_engine,
)
from sqlalchemy.pool import StaticPool

logger = logging.getLogger(__name__)

# ── 엔진 ──────────────────────────────────────────────────────────

_DATABASE_URL = os.getenv("DATABASE_URL", "")


def _make_engine():
    if _DATABASE_URL:
        # Supabase / Heroku: postgres:// → postgresql://
        url = _DATABASE_URL.replace("postgres://", "postgresql://", 1)
        logger.info("[DB] PostgreSQL 연결: %s", url[:40] + "...")
        return create_engine(
            url,
            pool_pre_ping=True,
            pool_size=3,
            max_overflow=7,
        )
    # 로컬 SQLite fallback
    db_path = Path(__file__).parent.parent / "data" / "database.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("[DB] SQLite 로컬: %s", db_path)
    return create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


engine = _make_engine()
metadata = MetaData()

# ── 테이블 정의 ────────────────────────────────────────────────────

reports = Table("reports", metadata,
    Column("id",               Integer, primary_key=True, autoincrement=True),
    Column("date",             Text,    nullable=False),
    Column("run_type",         Text,    nullable=False),
    Column("ceo_report",       Text),
    Column("candidates",       Text),
    Column("sector_scores",    Text),
    Column("market_direction", Text),
    Column("created_at",       Text,    server_default="CURRENT_TIMESTAMP"),
)

reviews = Table("reviews", metadata,
    Column("id",             Integer, primary_key=True, autoincrement=True),
    Column("date",           Text,    nullable=False),
    Column("review_content", Text),
    Column("created_at",     Text,    server_default="CURRENT_TIMESTAMP"),
)

midterm_reports = Table("midterm_reports", metadata,
    Column("id",         Integer, primary_key=True, autoincrement=True),
    Column("date",       Text,    nullable=False),
    Column("report",     Text),
    Column("created_at", Text,    server_default="CURRENT_TIMESTAMP"),
)

longterm_reports = Table("longterm_reports", metadata,
    Column("id",         Integer, primary_key=True, autoincrement=True),
    Column("date",       Text,    nullable=False),
    Column("report",     Text),
    Column("created_at", Text,    server_default="CURRENT_TIMESTAMP"),
)

stock_recommendations = Table("stock_recommendations", metadata,
    Column("id",           Integer, primary_key=True, autoincrement=True),
    Column("date",         Text,    nullable=False),
    Column("code",         Text,    nullable=False),
    Column("name",         Text,    nullable=False),
    Column("entry_price",  Float),
    Column("stop_price",   Float),
    Column("target_price", Float),
    Column("rationale",    Text),
    Column("close_price",  Float),
    Column("return_pct",   Float),
    Column("result",       Text),
    Column("created_at",   Text,    server_default="CURRENT_TIMESTAMP"),
)

foreign_buy_history = Table("foreign_buy_history", metadata,
    Column("date",   Text,  primary_key=True),
    Column("code",   Text,  primary_key=True),
    Column("name",   Text),
    Column("amount", Float),
)

portfolio_positions = Table("portfolio_positions", metadata,
    Column("id",           Integer, primary_key=True, autoincrement=True),
    Column("code",         Text,    nullable=False, unique=True),
    Column("name",         Text,    nullable=False),
    Column("quantity",     Integer, nullable=False, default=0),
    Column("avg_price",    Float,   nullable=False),
    Column("entry_date",   Text),
    Column("timeframe",    Text,    default="short"),
    Column("sector",       Text),
    Column("target_price", Float),
    Column("stop_price",   Float),
    Column("memo",         Text),
    Column("status",       Text,    default="holding"),
    Column("created_at",   Text,    server_default="CURRENT_TIMESTAMP"),
    Column("updated_at",   Text,    server_default="CURRENT_TIMESTAMP"),
)

portfolio_history = Table("portfolio_history", metadata,
    Column("id",         Integer, primary_key=True, autoincrement=True),
    Column("code",       Text,    nullable=False),
    Column("name",       Text,    nullable=False),
    Column("quantity",   Integer),
    Column("avg_price",  Float),
    Column("exit_price", Float),
    Column("exit_date",  Text),
    Column("return_pct", Float),
    Column("timeframe",  Text),
    Column("memo",       Text),
    Column("created_at", Text,    server_default="CURRENT_TIMESTAMP"),
)

watchlist_items = Table("watchlist_items", metadata,
    Column("id",            Integer, primary_key=True, autoincrement=True),
    Column("code",          Text,    nullable=False, unique=True),
    Column("name",          Text,    nullable=False),
    Column("target_entry",  Float),
    Column("timeframe",     Text,    default="short"),
    Column("reason",        Text),
    Column("trigger_type",  Text,    default="price_below"),
    Column("trigger_value", Float),
    Column("priority",      Text,    default="normal"),
    Column("status",        Text,    default="active"),
    Column("added_date",    Text),
    Column("created_at",    Text,    server_default="CURRENT_TIMESTAMP"),
)

dart_sent_alerts = Table("dart_sent_alerts", metadata,
    Column("rcept_no",  Text, primary_key=True),
    Column("corp_name", Text),
    Column("report_nm", Text),
    Column("date",      Text),
    Column("sent_at",   Text, server_default="CURRENT_TIMESTAMP"),
)

bigfigure_alert_log = Table("bigfigure_alert_log", metadata,
    Column("date",    Text, primary_key=True),
    Column("sent_at", Text, server_default="CURRENT_TIMESTAMP"),
)

price_alert_log = Table("price_alert_log", metadata,
    Column("date",    Text, primary_key=True),
    Column("code",    Text, primary_key=True),
    Column("type",    Text, primary_key=True),
    Column("sent_at", Text, server_default="CURRENT_TIMESTAMP"),
)

us_invest_recommendations = Table("us_invest_recommendations", metadata,
    Column("id",         Integer, primary_key=True, autoincrement=True),
    Column("date",       Text,    nullable=False),
    Column("category",   Text,    nullable=False),
    Column("ticker",     Text,    nullable=False),
    Column("name",       Text),
    Column("price",      Float),
    Column("change_1w",  Float),
    Column("change_1m",  Float),
    Column("score",      Float),
    Column("rationale",  Text),
    Column("created_at", Text,    server_default="CURRENT_TIMESTAMP"),
)

# ── 데이터 축적 테이블 ──────────────────────────────────────────────

market_snapshots = Table("market_snapshots", metadata,
    Column("id",          Integer, primary_key=True, autoincrement=True),
    Column("date",        Text,    nullable=False),
    Column("run_type",    Text,    nullable=False),
    Column("kospi",       Float),
    Column("kospi_chg",   Float),
    Column("kosdaq",      Float),
    Column("kosdaq_chg",  Float),
    Column("usd_krw",     Float),
    Column("vix",         Float),
    Column("oil_wti",     Float),
    Column("gold",        Float),
    Column("us10y",       Float),
    Column("sp500_fut",   Float),
    Column("sp500_chg",   Float),
    Column("nasdaq_fut",  Float),
    Column("nasdaq_chg",  Float),
    Column("created_at",  Text,    server_default="CURRENT_TIMESTAMP"),
)

intelligence_archive = Table("intelligence_archive", metadata,
    Column("id",          Integer, primary_key=True, autoincrement=True),
    Column("date",        Text,    nullable=False),
    Column("run_type",    Text,    nullable=False),
    Column("source_type", Text),   # blog / telegram / global / securities
    Column("summary",     Text),   # 핵심 인사이트 요약 텍스트
    Column("sentiment",   Text),   # 강세 / 약세 / 중립
    Column("key_themes",  Text),   # 쉼표 구분 키워드
    Column("created_at",  Text,    server_default="CURRENT_TIMESTAMP"),
)

# ── 초기화 ─────────────────────────────────────────────────────────

def init_db():
    """모든 테이블 생성 (존재하면 스킵). 애플리케이션 시작 시 한 번 호출."""
    metadata.create_all(engine, checkfirst=True)
    logger.info("[DB] 테이블 초기화 완료")


# ── 연결 컨텍스트 매니저 ───────────────────────────────────────────

@contextlib.contextmanager
def get_conn():
    """트랜잭션 컨텍스트 매니저.

    사용:
        from db.database import get_conn
        from sqlalchemy import text

        with get_conn() as conn:
            conn.execute(text("INSERT INTO ..."), {"col": val})
            rows = conn.execute(text("SELECT ...")).fetchall()
        # with 블록 종료 시 자동 commit, 예외 시 rollback
    """
    with engine.begin() as conn:
        yield conn
