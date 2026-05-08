import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

_DB = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data", "database.sqlite3"))


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB), exist_ok=True)
    return sqlite3.connect(_DB)


def init_db():
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS reports (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                date             TEXT NOT NULL,
                run_type         TEXT NOT NULL,
                ceo_report       TEXT,
                candidates       TEXT,
                sector_scores    TEXT,
                market_direction TEXT,
                created_at       TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS reviews (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                date            TEXT NOT NULL,
                review_content  TEXT,
                created_at      TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS midterm_reports (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                date       TEXT NOT NULL,
                report     TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS longterm_reports (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                date       TEXT NOT NULL,
                report     TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS stock_recommendations (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                date         TEXT NOT NULL,
                code         TEXT NOT NULL,
                name         TEXT NOT NULL,
                entry_price  REAL,
                stop_price   REAL,
                target_price REAL,
                rationale    TEXT,
                close_price  REAL,
                return_pct   REAL,
                result       TEXT,
                created_at   TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS foreign_buy_history (
                date   TEXT NOT NULL,
                code   TEXT NOT NULL,
                name   TEXT,
                amount REAL,
                PRIMARY KEY (date, code)
            );
        """)
    logger.info("DB 초기화 완료")


def save_review(date: str, content: str):
    with _conn() as c:
        c.execute("INSERT INTO reviews (date, review_content) VALUES (?, ?)", (date, content))


def save_midterm_report(date: str, report: str):
    with _conn() as c:
        c.execute("INSERT INTO midterm_reports (date, report) VALUES (?, ?)", (date, report))


def save_longterm_report(date: str, report: str):
    with _conn() as c:
        c.execute("INSERT INTO longterm_reports (date, report) VALUES (?, ?)", (date, report))


def get_last_close_report() -> dict | None:
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT date, ceo_report, market_direction FROM reports "
                "WHERE run_type='close_market' ORDER BY date DESC LIMIT 1"
            ).fetchone()
        if row:
            return {"date": row[0], "ceo_report": row[1], "market_direction": row[2]}
    except Exception as e:
        logger.warning("이전 리포트 조회 실패: %s", e)
    return None
