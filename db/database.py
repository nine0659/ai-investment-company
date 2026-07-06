"""
db/database.py — 중앙 데이터베이스 레이어

DATABASE_URL 환경변수:
  설정 시  → Neon PostgreSQL (운영)
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
    Column, Float, Integer, MetaData, String, Table, Text, UniqueConstraint,
    create_engine, text,
)
from sqlalchemy.pool import StaticPool

logger = logging.getLogger(__name__)

# ── 엔진 ──────────────────────────────────────────────────────────

_DATABASE_URL = os.getenv("DATABASE_URL", "")


def _make_sqlite() -> "Engine":
    db_path = Path(__file__).parent.parent / "data" / "database.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("[DB] SQLite 로컬: %s", db_path)
    return create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _make_engine() -> "Engine":
    if _DATABASE_URL:
        # Neon / Heroku: postgres:// → postgresql://
        url = _DATABASE_URL.replace("postgres://", "postgresql://", 1)
        logger.info("[DB] PostgreSQL 연결 시도: %s", url[:40] + "...")
        try:
            engine = create_engine(
                url,
                pool_pre_ping=True,
                pool_size=3,
                max_overflow=7,
                connect_args={"connect_timeout": 10},
            )
            # 실제 연결 테스트
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("[DB] PostgreSQL 연결 성공")
            return engine
        except Exception as e:
            # 조용한 폴백 금지 — 운영에서 SQLite로 넘어가면 재시작마다 데이터가 증발한다.
            # (포트폴리오·투자관·성과 추적 전부 소실) 반드시 크게 알리고 넘어간다.
            logger.critical(
                "[DB] ⚠️ PostgreSQL 연결 실패 → 임시 SQLite로 전환. "
                "저장 데이터는 재시작 시 소실됩니다. DATABASE_URL 즉시 점검 필요: %s", e
            )
            _alert_db_fallback(e)
            return _make_sqlite()
    return _make_sqlite()


def _alert_db_fallback(err: Exception) -> None:
    """PostgreSQL 폴백 발생을 텔레그램으로 즉시 통보 (실패해도 부팅은 계속)."""
    try:
        import requests
        token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": (
                    "🚨 [시스템] 운영 DB(PostgreSQL) 연결 실패 — 임시 SQLite로 동작 중\n\n"
                    "포트폴리오·투자관·성과 기록이 재시작 시 소실됩니다.\n"
                    "Neon 프로젝트 상태와 Render/GitHub Actions의 DATABASE_URL을 즉시 확인하세요.\n\n"
                    f"오류: {str(err)[:200]}"
                ),
            },
            timeout=10,
        )
    except Exception:
        pass  # 알림 실패가 부팅을 막으면 안 됨


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
    Column("created_at",       Text,    server_default=text("CURRENT_TIMESTAMP")),
)

# 브리핑 파이프라인 중복 실행 방지용 원자적 선점 테이블.
# 스케줄러 cron · 웹 대시보드 수동실행 · 재시작 복구 스레드 등 여러 경로가
# 같은 (date, run_type) 브리핑을 동시에 시작하려 할 때, UNIQUE 제약으로
# 단 하나만 통과시킨다 (reports 테이블 INSERT는 파이프라인 끝에서야 일어나
# 그 전까지는 중복 트리거를 막지 못했음 — 2026-06-24 중복발송 원인).
report_claims = Table("report_claims", metadata,
    Column("date",       Text, nullable=False),
    Column("run_type",   Text, nullable=False),
    Column("claimed_at", Text, server_default=text("CURRENT_TIMESTAMP")),
    UniqueConstraint("date", "run_type", name="uq_report_claims_date_run_type"),
)

# 메인 텔레그램 브리핑은 짧은 결론(헤드라인+액션)만 담는다 — 글로벌 시장 서사,
# 전문가·텔레그램 채널 시각, 종목별 기술적·수급 분석처럼 압축 과정에서 잘려나가는
# 내용은 여기에 보존해 /insight 명령어·대시보드에서 조회한다 (2026-06-24).
deep_reports = Table("deep_reports", metadata,
    Column("id",         Integer, primary_key=True, autoincrement=True),
    Column("date",       Text,    nullable=False),
    Column("run_type",   Text,    nullable=False),
    Column("content",    Text),
    Column("created_at", Text,    server_default=text("CURRENT_TIMESTAMP")),
)

reviews = Table("reviews", metadata,
    Column("id",             Integer, primary_key=True, autoincrement=True),
    Column("date",           Text,    nullable=False),
    Column("review_content", Text),
    Column("created_at",     Text,    server_default=text("CURRENT_TIMESTAMP")),
)

midterm_reports = Table("midterm_reports", metadata,
    Column("id",         Integer, primary_key=True, autoincrement=True),
    Column("date",       Text,    nullable=False),
    Column("report",     Text),
    Column("created_at", Text,    server_default=text("CURRENT_TIMESTAMP")),
)

longterm_reports = Table("longterm_reports", metadata,
    Column("id",         Integer, primary_key=True, autoincrement=True),
    Column("date",       Text,    nullable=False),
    Column("report",     Text),
    Column("created_at", Text,    server_default=text("CURRENT_TIMESTAMP")),
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
    Column("created_at",   Text,    server_default=text("CURRENT_TIMESTAMP")),
)

foreign_buy_history = Table("foreign_buy_history", metadata,
    Column("date",   Text,  primary_key=True),
    Column("code",   Text,  primary_key=True),
    Column("name",   Text),
    Column("amount", Float),
)

portfolio_positions = Table("portfolio_positions", metadata,
    Column("id",           Integer, primary_key=True, autoincrement=True),
    Column("code",         Text,    nullable=False),
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
    Column("created_at",   Text,    server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at",   Text,    server_default=text("CURRENT_TIMESTAMP")),
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
    Column("created_at", Text,    server_default=text("CURRENT_TIMESTAMP")),
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
    Column("created_at",    Text,    server_default=text("CURRENT_TIMESTAMP")),
)

dart_sent_alerts = Table("dart_sent_alerts", metadata,
    Column("rcept_no",  Text, primary_key=True),
    Column("corp_name", Text),
    Column("report_nm", Text),
    Column("date",      Text),
    Column("sent_at",   Text, server_default=text("CURRENT_TIMESTAMP")),
)

bigfigure_alert_log = Table("bigfigure_alert_log", metadata,
    Column("date",    Text, primary_key=True),
    Column("sent_at", Text, server_default=text("CURRENT_TIMESTAMP")),
)

price_alert_log = Table("price_alert_log", metadata,
    Column("date",    Text, primary_key=True),
    Column("code",    Text, primary_key=True),
    Column("type",    Text, primary_key=True),
    Column("sent_at", Text, server_default=text("CURRENT_TIMESTAMP")),
)

# 장중 KOSPI 등락률 변동 추적 — 급반전(트렌드 역전) 감지용 (긴급모니터 5분마다 갱신)
intraday_extremes = Table("intraday_extremes", metadata,
    Column("date",             Text,    primary_key=True),
    Column("min_kospi_chg",    Float),
    Column("max_kospi_chg",    Float),
    Column("reversal_alerted", Integer, default=0),
    Column("updated_at",       Text,    server_default=text("CURRENT_TIMESTAMP")),
)

alert_notifications = Table("alert_notifications", metadata,
    Column("id",         Integer, primary_key=True, autoincrement=True),
    Column("date",       Text,    nullable=False),
    Column("alert_type", Text,    nullable=False),  # entry / stop / target / volume / golden_cross
    Column("code",       Text,    nullable=False),
    Column("name",       Text),
    Column("message",    Text),
    Column("created_at", Text,    server_default=text("CURRENT_TIMESTAMP")),
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
    Column("created_at", Text,    server_default=text("CURRENT_TIMESTAMP")),
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
    Column("created_at",  Text,    server_default=text("CURRENT_TIMESTAMP")),
)

investment_thesis = Table("investment_thesis", metadata,
    Column("id",              Integer, primary_key=True, autoincrement=True),
    Column("date",            Text,    nullable=False),   # YYYY-MM-DD
    Column("cycle_stage",     Text),   # 경기 사이클: 초기확장/중기확장/후기확장/수축
    Column("macro_regime",    Text),   # RISK-ON / RISK-OFF / NEUTRAL
    Column("outlook_6m",      Text),   # 6개월 전망 요약
    Column("outlook_12m",     Text),   # 12개월 전망 요약
    Column("sector_overweight",  Text),  # 비중 확대 섹터 (JSON 리스트)
    Column("sector_underweight", Text),  # 비중 축소 섹터 (JSON 리스트)
    Column("conviction_ideas",   Text),  # 핵심 확신 아이디어 (JSON)
    Column("bull_scenario",   Text),   # 강세 시나리오 + 확률
    Column("base_scenario",   Text),   # 기본 시나리오 + 확률
    Column("bear_scenario",   Text),   # 약세 시나리오 + 확률
    Column("invalidation",    Text),   # 투자 근거 무효 조건
    Column("full_report",     Text),   # 전체 리포트 원문
    Column("ceo_summary",     Text),   # CEO 일일 주입용 압축 요약 (~600자)
    Column("created_at",      Text,    server_default=text("CURRENT_TIMESTAMP")),
)

strategy_reports = Table("strategy_reports", metadata,
    Column("id",           Integer, primary_key=True, autoincrement=True),
    Column("date",         Text,    nullable=False),   # 실행 날짜 (YYYY-MM-DD)
    Column("report_type",  Text,    default="weekly"), # weekly / longterm
    Column("report",       Text),                      # 전체 전략 리포트
    Column("ceo_summary",  Text),                      # CEO 일일 브리핑 주입용 압축 요약 (~500자)
    Column("created_at",   Text,    server_default=text("CURRENT_TIMESTAMP")),
)

intelligence_archive = Table("intelligence_archive", metadata,
    Column("id",          Integer, primary_key=True, autoincrement=True),
    Column("date",        Text,    nullable=False),
    Column("run_type",    Text,    nullable=False),
    Column("source_type", Text),   # blog / telegram / global / securities
    Column("summary",     Text),   # 핵심 인사이트 요약 텍스트
    Column("sentiment",   Text),   # 강세 / 약세 / 중립
    Column("key_themes",  Text),   # 쉼표 구분 키워드
    Column("created_at",  Text,    server_default=text("CURRENT_TIMESTAMP")),
)

attribution_log = Table("attribution_log", metadata,
    Column("id",            Integer, primary_key=True, autoincrement=True),
    Column("week_end",      Text,    nullable=False),  # 분석 기준 주 마지막날 (YYYY-MM-DD)
    Column("macro_score",   Float),  # 매크로 판단 정확도 점수 (0~10)
    Column("sector_score",  Float),  # 섹터 선택 점수
    Column("stock_score",   Float),  # 종목 선택 점수
    Column("timing_score",  Float),  # 타이밍 점수
    Column("thesis_score",  Float),  # 투자관 부합도 점수
    Column("total_score",   Float),  # 종합 점수
    Column("key_learnings", Text),   # 핵심 교훈 (다음 투자에 반영할 내용)
    Column("full_report",   Text),   # 전체 귀인 분석 리포트
    Column("created_at",    Text,    server_default=text("CURRENT_TIMESTAMP")),
)

portfolio_nav = Table("portfolio_nav", metadata,
    Column("id",            Integer, primary_key=True, autoincrement=True),
    Column("date",          Text,    nullable=False, unique=True),  # YYYY-MM-DD
    Column("total_value",   Float),   # 포트폴리오 총 평가금액 (원)
    Column("total_cost",    Float),   # 포트폴리오 총 매입금액 (원)
    Column("total_pnl",     Float),   # 총 손익금액 (원)
    Column("total_pnl_pct", Float),   # 총 손익률 (%)
    Column("kospi_close",   Float),   # 당일 KOSPI 종가
    Column("kospi_pct_ytd", Float),   # KOSPI 연초 대비 등락률 (%)
    Column("nav_pct_ytd",   Float),   # 포트폴리오 연초 대비 등락률 (%)
    Column("alpha_ytd",     Float),   # 초과수익률 = nav_pct_ytd - kospi_pct_ytd
    Column("position_count",Integer), # 보유 종목 수
    Column("created_at",    Text,     server_default=text("CURRENT_TIMESTAMP")),
)

order_history = Table("order_history", metadata,
    Column("id",         Integer, primary_key=True, autoincrement=True),
    Column("created_at", Text,    server_default=text("CURRENT_TIMESTAMP")),
    Column("code",       Text,    nullable=False),
    Column("name",       Text),
    Column("side",       Text,    nullable=False),   # buy | sell
    Column("qty",        Integer, nullable=False),
    Column("price",      Integer, nullable=False),
    Column("amount",     Integer),                   # qty * price
    Column("order_no",   Text),                      # KIS 주문번호
    Column("mode",       Text),                      # 실계좌 | 모의계좌
    Column("success",    Integer, default=0),        # 1=성공, 0=실패
    Column("message",    Text),
    Column("memo",       Text),
    Column("rec_id",     Integer),                   # stock_recommendations.id 참조 (자동실행 추적)
)

# 스케줄 잡 실행 대장 — "조용한 실패"(잡이 아예 안 돌았는데 아무도 모름) 감지용.
# 매일 아침 헬스체크가 전날 예정 잡의 기록 유무를 대조해 누락·실패를 경보한다.
job_runs = Table("job_runs", metadata,
    Column("id",         Integer, primary_key=True, autoincrement=True),
    Column("date",       Text,    nullable=False),   # YYYY-MM-DD (KST)
    Column("job_name",   Text,    nullable=False),   # daily_nav, weekly_strategy ...
    Column("status",     Text,    nullable=False),   # success | fail | skipped
    Column("detail",     Text),                      # 실패 사유·스킵 사유
    Column("created_at", Text,    server_default=text("CURRENT_TIMESTAMP")),
)

# ── 시스템 설정 (DB 기반, 프로세스 간 공유) ───────────────────────
system_settings = Table("system_settings", metadata,
    Column("key",        Text, primary_key=True),   # 설정 키
    Column("value",      Text, nullable=False),     # 설정 값
    Column("updated_at", Text, server_default=text("CURRENT_TIMESTAMP")),
)

# ── 자동 실행 일시 중단 플래그 ─────────────────────────────────────
auto_execute_pause = Table("auto_execute_pause", metadata,
    Column("id",         Integer, primary_key=True, autoincrement=True),
    Column("reason",     Text),
    Column("pause_until",Text),   # YYYY-MM-DD HH:MM:SS
    Column("created_at", Text,    server_default=text("CURRENT_TIMESTAMP")),
)

# ── AI 성과 추적 테이블 ────────────────────────────────────────────

recommendation_tracking = Table("recommendation_tracking", metadata,
    Column("id",           Integer, primary_key=True, autoincrement=True),
    Column("rec_id",       Integer, nullable=False),   # stock_recommendations.id 참조
    Column("date",         Text,    nullable=False),   # 추적 날짜 (YYYY-MM-DD)
    Column("code",         Text,    nullable=False),
    Column("name",         Text),
    Column("rec_date",     Text,    nullable=False),   # 최초 추천 날짜
    Column("entry_price",  Float),                     # 추천 당시 진입가
    Column("stop_price",   Float),                     # 손절가
    Column("target_price", Float),                     # 목표가
    Column("current_price",Float),                     # 당일 종가
    Column("return_pct",   Float),                     # 추천일 대비 수익률 (%)
    Column("max_return",   Float),                     # 추적 기간 최고 수익률
    Column("min_return",   Float),                     # 추적 기간 최저 수익률
    Column("days_held",    Integer),                   # 추천일로부터 경과 영업일
    Column("status",       Text,    default="tracking"),  # tracking / target_hit / stop_hit / expired
    Column("created_at",   Text,    server_default=text("CURRENT_TIMESTAMP")),
)

market_predictions = Table("market_predictions", metadata,
    Column("id",              Integer, primary_key=True, autoincrement=True),
    Column("date",            Text,    nullable=False),   # 예측 날짜 (YYYY-MM-DD)
    Column("run_type",        Text,    nullable=False),   # pre_market / close_market
    Column("predicted_dir",   Text),                      # 상승 / 하락 / 중립 (CEO 예측)
    Column("predicted_prob",  Float),                     # 예측 확률 (%) — "상승 75%"에서 파싱
    Column("actual_kospi",    Float),                     # 실제 KOSPI 등락률 (%)
    Column("actual_dir",      Text),                      # 실제 방향 (상승/하락/중립)
    Column("correct",         Integer),                   # 1=적중, 0=실패, NULL=미검증
    Column("sector_pred",     Text),                      # 예측 주도 섹터
    Column("created_at",      Text,    server_default=text("CURRENT_TIMESTAMP")),
)

# ── 초기화 ─────────────────────────────────────────────────────────

def init_db():
    """모든 테이블 생성 (존재하면 스킵). 애플리케이션 시작 시 한 번 호출."""
    metadata.create_all(engine, checkfirst=True)
    _migrate_order_history()
    try:
        from services.position_lifecycle_service import migrate_portfolio_positions
        migrate_portfolio_positions()
    except Exception as _me:
        logger.warning("[DB] 포지션 생애주기 마이그레이션 실패: %s", _me)
    try:
        from services.prediction_service import migrate_market_predictions
        migrate_market_predictions()
    except Exception as _me2:
        logger.warning("[DB] 예측 테이블 마이그레이션 실패: %s", _me2)
    logger.info("[DB] 테이블 초기화 완료")


def _migrate_order_history():
    """order_history 테이블에 rec_id 컬럼이 없으면 ALTER TABLE로 추가."""
    try:
        # inspector를 engine.begin() 밖에서 생성해야 SQLite에서 올바르게 작동
        from sqlalchemy import inspect as sa_inspect
        inspector = sa_inspect(engine)
        cols = [c["name"] for c in inspector.get_columns("order_history")]
        if "rec_id" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE order_history ADD COLUMN rec_id INTEGER"))
            logger.info("[DB] order_history.rec_id 컬럼 추가 완료")
        else:
            logger.debug("[DB] order_history.rec_id 이미 존재 — 스킵")
    except Exception as e:
        logger.warning("[DB] order_history 마이그레이션 실패: %s", e)


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
