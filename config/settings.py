import os
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

TIMEZONE_STR = "Asia/Seoul"
TZ = ZoneInfo(TIMEZONE_STR)

RUN_TYPE_GLOBAL = "global_market"   # 미국 장 마감 후 글로벌 시황 (06:30)
RUN_TYPE_PRE    = "pre_market"
RUN_TYPE_INTRA1 = "intra_market_1"
RUN_TYPE_INTRA2 = "intra_market_2"
RUN_TYPE_CLOSE  = "close_market"

SCHEDULE_GLOBAL     = "06:30"   # 미국 장 마감 후 글로벌 시황 브리핑
SCHEDULE_PRE_MARKET = "08:20"
SCHEDULE_INTRA_1    = "10:00"
SCHEDULE_INTRA_2    = "13:00"
SCHEDULE_CLOSE      = "16:30"   # 수급 데이터 완전 집계 후 (기존 15:50 → 16:30)

OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL     = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_MODEL_CEO = os.getenv("OPENAI_MODEL_CEO", OPENAI_MODEL)  # CEO 전용 모델

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

KIS_APP_KEY         = os.getenv("KIS_APP_KEY", "")
KIS_APP_SECRET      = os.getenv("KIS_APP_SECRET", "")
KIS_ACCOUNT_NO      = os.getenv("KIS_ACCOUNT_NO", "")
KIS_ACCOUNT_PROD_CD = os.getenv("KIS_ACCOUNT_PROD_CD", "01")

DART_API_KEY = os.getenv("DART_API_KEY", "")

# ── 카카오톡 긴급 알림 (선택) ─────────────────────────────────────
# 발급: https://developers.kakao.com → 앱 생성 → 카카오 로그인 → 토큰 발급
# 미설정 시 텔레그램으로만 발송
KAKAO_ACCESS_TOKEN = os.getenv("KAKAO_ACCESS_TOKEN", "")

# ── 데이터베이스 ─────────────────────────────────────────────────
# 설정 시 PostgreSQL(Supabase), 미설정 시 SQLite 로컬
DATABASE_URL = os.getenv("DATABASE_URL", "")

KIS_IS_REAL  = KIS_ACCOUNT_PROD_CD == "01"
KIS_BASE_URL = (
    "https://openapi.koreainvestment.com:9443"
    if KIS_IS_REAL
    else "https://openapivts.koreainvestment.com:29443"
)

# KIS_IS_REAL 오설정 조기 경고 — 모의투자/실전계좌 의도치 않은 전환 방지
if KIS_ACCOUNT_PROD_CD and KIS_ACCOUNT_PROD_CD not in ("01", "02"):
    import warnings
    warnings.warn(
        f"KIS_ACCOUNT_PROD_CD='{KIS_ACCOUNT_PROD_CD}' — 올바른 값은 '01'(실전) 또는 '02'(모의). "
        f"현재 KIS_IS_REAL={KIS_IS_REAL}로 설정됨.",
        UserWarning,
        stacklevel=2,
    )


# KIS API가 필요하지 않은 실행 타입 (미국주식·주간통계·월간학습·DART는 KIS 불필요)
_NO_KIS_TYPES = {"us-invest", "weekly", "monthly", "dart"}


# ── 자동 실행 설정 (DB 우선, .env 폴백, 기본값 false — 안전 우선) ──
AUTO_EXECUTE_BUY          = os.getenv("AUTO_EXECUTE_BUY", "false").lower() == "true"
AUTO_EXECUTE_STOP         = os.getenv("AUTO_EXECUTE_STOP", "false").lower() == "true"
AUTO_EXECUTE_TARGET_HALF  = os.getenv("AUTO_EXECUTE_TARGET_HALF", "false").lower() == "true"
AUTO_MAX_DAILY_EXPOSURE   = float(os.getenv("AUTO_MAX_DAILY_EXPOSURE", "0.10"))
AUTO_SIZE_MAP: dict[str, float] = {"상": 0.05, "중": 0.03, "하": 0.01}

# DB 기반 설정 헬퍼 — 프로세스 경계를 넘어 설정 공유
_SETTING_KEYS = ("AUTO_EXECUTE_BUY", "AUTO_EXECUTE_STOP", "AUTO_EXECUTE_TARGET_HALF")


def get_auto_setting(key: str) -> bool:
    """DB system_settings → 환경변수 순으로 자동실행 설정값 반환."""
    try:
        from db.database import get_conn
        from sqlalchemy import text
        with get_conn() as conn:
            row = conn.execute(
                text("SELECT value FROM system_settings WHERE key=:k"), {"k": key}
            ).fetchone()
        if row:
            return row[0].lower() == "true"
    except Exception:
        pass
    return os.getenv(key, "false").lower() == "true"


def set_auto_setting(key: str, value: bool) -> None:
    """DB system_settings에 자동실행 설정 저장 (프로세스 간 공유)."""
    val_str = "true" if value else "false"
    try:
        from db.database import get_conn
        from sqlalchemy import text
        with get_conn() as conn:
            conn.execute(text("""
                INSERT INTO system_settings (key, value, updated_at)
                VALUES (:k, :v, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value=:v, updated_at=CURRENT_TIMESTAMP
            """), {"k": key, "v": val_str})
    except Exception as _e:
        import logging
        logging.getLogger(__name__).warning("[설정] DB 저장 실패, 메모리만 갱신: %s", _e)
    # 현재 프로세스 메모리도 즉시 갱신
    import sys
    mod = sys.modules.get(__name__)
    if mod and hasattr(mod, key):
        setattr(mod, key, value)


def validate_env(run_type: str = "") -> list[str]:
    required: dict[str, str] = {
        "OPENAI_API_KEY":     OPENAI_API_KEY,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID":   TELEGRAM_CHAT_ID,
    }
    if run_type not in _NO_KIS_TYPES:
        required.update({
            "KIS_APP_KEY":    KIS_APP_KEY,
            "KIS_APP_SECRET": KIS_APP_SECRET,
            "KIS_ACCOUNT_NO": KIS_ACCOUNT_NO,
        })
    return [k for k, v in required.items() if not v or v in ("...", "sk-...")]
