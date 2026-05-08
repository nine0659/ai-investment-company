"""
DART 공시 실시간 감지 에이전트
30분마다 실행 → 중요 공시 감지 시 텔레그램 즉시 알림
"""
import logging
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from config.settings import DART_API_KEY
from clients.telegram_client import send_message

logger = logging.getLogger(__name__)

_KST  = ZoneInfo("Asia/Seoul")
_BASE = "https://opendart.fss.or.kr/api"
_DB   = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data", "database.sqlite3"))

# 감지할 공시 유형 코드
_PBLNTF_TYPES = ["A", "B", "D", "F"]   # 정기·주요·지분·외부감사

# 중요 키워드 → 알림 조건
_ALERT_RULES = [
    {
        "keywords": ["수주", "공급계약", "납품계약", "매출액의"],
        "min_amount": 10_000_000_000,   # 100억
        "emoji": "🏆", "label": "대규모 수주/계약",
    },
    {
        "keywords": ["자기주식취득", "자기주식 취득", "자사주"],
        "min_amount": 0,
        "emoji": "💰", "label": "자사주 매입",
    },
    {
        "keywords": ["영업이익", "매출액", "잠정실적", "실적발표"],
        "min_amount": 0,
        "emoji": "📈", "label": "실적 발표",
    },
    {
        "keywords": ["주요사항보고", "최대주주변경", "합병", "인수"],
        "min_amount": 0,
        "emoji": "⚡", "label": "주요 이벤트",
    },
]


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB), exist_ok=True)
    return sqlite3.connect(_DB)


def _ensure_table():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS dart_seen (
                rcept_no TEXT PRIMARY KEY,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)


def _is_seen(rcept_no: str) -> bool:
    with _conn() as c:
        row = c.execute("SELECT 1 FROM dart_seen WHERE rcept_no=?", (rcept_no,)).fetchone()
    return row is not None


def _mark_seen(rcept_no: str):
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO dart_seen (rcept_no) VALUES (?)", (rcept_no,))


def fetch_today_disclosures() -> list[dict]:
    """오늘 자 DART 공시 목록 수집"""
    today = datetime.now(_KST).strftime("%Y%m%d")
    items = []
    for ptype in _PBLNTF_TYPES:
        try:
            r = requests.get(
                f"{_BASE}/list.json",
                params={
                    "crtfc_key": DART_API_KEY,
                    "bgn_de": today,
                    "end_de": today,
                    "pblntf_ty": ptype,
                    "page_count": 40,
                },
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("status") == "000":
                items.extend(data.get("list", []))
        except Exception as e:
            logger.debug("DART 공시 조회 실패 (%s): %s", ptype, e)
    return items


def _matches_rule(title: str, rule: dict) -> bool:
    return any(kw in title for kw in rule["keywords"])


def _extract_amount(title: str) -> int:
    """공시 제목에서 금액(원) 추출 시도"""
    import re
    # "XXX억원", "XXX백억" 등 패턴
    m = re.search(r"([\d,]+)\s*억", title)
    if m:
        return int(m.group(1).replace(",", "")) * 100_000_000
    m = re.search(r"([\d,]+)\s*원", title)
    if m:
        return int(m.group(1).replace(",", ""))
    return 0


def check_and_alert() -> int:
    """새 공시 체크 및 알림 발송. 발송 건수 반환."""
    _ensure_table()
    disclosures = fetch_today_disclosures()
    sent = 0

    for item in disclosures:
        rcept_no   = item.get("rcept_no", "")
        corp_name  = item.get("corp_name", "")
        report_nm  = item.get("report_nm", "")
        rcept_dt   = item.get("rcept_dt", "")

        if not rcept_no or _is_seen(rcept_no):
            continue

        for rule in _ALERT_RULES:
            if _matches_rule(report_nm, rule):
                amount = _extract_amount(report_nm)
                if rule["min_amount"] > 0 and amount < rule["min_amount"]:
                    continue

                msg = (
                    f"{rule['emoji']} *DART 공시 알림 [{rule['label']}]*\n"
                    f"기업: {corp_name}\n"
                    f"공시: {report_nm}\n"
                    f"접수: {rcept_dt[:4]}-{rcept_dt[4:6]}-{rcept_dt[6:8]} "
                    f"{rcept_dt[8:10]}:{rcept_dt[10:12]}\n"
                    f"[DART 바로가기](https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no})"
                )
                try:
                    send_message(msg)
                    sent += 1
                    logger.info("DART 알림 발송: %s - %s", corp_name, report_nm)
                except Exception as e:
                    logger.warning("DART 알림 발송 실패: %s", e)
                break  # 첫 번째 매칭 룰에서 한 번만 알림

        _mark_seen(rcept_no)

    return sent


def run():
    """독립 실행 진입점"""
    now = datetime.now(_KST)
    logger.info("[DART 감지] 시작 — %s KST", now.strftime("%H:%M"))
    sent = check_and_alert()
    logger.info("[DART 감지] 완료 — 알림 %d건", sent)


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s %(message)s")
    run()
