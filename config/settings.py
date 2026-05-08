import os
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

TIMEZONE_STR = "Asia/Seoul"
TZ = ZoneInfo(TIMEZONE_STR)

RUN_TYPE_PRE    = "pre_market"
RUN_TYPE_INTRA1 = "intra_market_1"
RUN_TYPE_INTRA2 = "intra_market_2"
RUN_TYPE_CLOSE  = "close_market"

SCHEDULE_PRE_MARKET = "08:20"
SCHEDULE_INTRA_1    = "10:00"
SCHEDULE_INTRA_2    = "13:00"
SCHEDULE_CLOSE      = "15:50"

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

KIS_IS_REAL  = KIS_ACCOUNT_PROD_CD == "01"
KIS_BASE_URL = (
    "https://openapi.koreainvestment.com:9443"
    if KIS_IS_REAL
    else "https://openapivts.koreainvestment.com:29443"
)


def validate_env() -> list[str]:
    required = {
        "OPENAI_API_KEY":     OPENAI_API_KEY,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID":   TELEGRAM_CHAT_ID,
        "KIS_APP_KEY":        KIS_APP_KEY,
        "KIS_APP_SECRET":     KIS_APP_SECRET,
        "KIS_ACCOUNT_NO":     KIS_ACCOUNT_NO,
    }
    return [k for k, v in required.items() if not v or v in ("...", "sk-...")]
