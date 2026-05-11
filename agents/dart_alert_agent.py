"""
DART 공시 실시간 감지 에이전트
- 중복 방지: dart_sent_alerts (실제 발송한 공시만 저장, rcept_no 기준)
- 필터링: 코스피/코스닥 상장 대형주(시총 1000억+), 주요 공시 유형만
- 하루 최대 10건, 중요도 높은 순 발송
"""
import logging
import os
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from config.settings import DART_API_KEY, KIS_APP_KEY
from clients.telegram_client import send_message

logger = logging.getLogger(__name__)

_KST  = ZoneInfo("Asia/Seoul")
_BASE = "https://opendart.fss.or.kr/api"
_DB   = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data", "database.sqlite3"))

_DAILY_LIMIT = 10

# DART corp_cls: Y=KOSPI, K=KOSDAQ 상장사만 대상
_LISTED_CLS = {"Y", "K"}

# 시가총액 기준 (억원)
_MIN_MARKET_CAP_억 = 1_000   # 코스피200/코스닥150 프록시 — 1000억 미만 소형주 제외
_LARGE_CAP_억      = 5_000   # 유상증자 전용 대형주 기준

# 감지할 공시 유형: A=정기공시(실적), B=주요사항보고(수주·부도·증자 등)
_PBLNTF_TYPES = ["A", "B"]

# 알림 규칙 (priority 낮을수록 먼저 발송)
_ALERT_RULES = [
    {
        "keywords": ["부도", "파산", "상장폐지", "관리종목지정"],
        "min_amount": 0,
        "large_cap_only": False,
        "priority": 1,
        "emoji": "🚨", "label": "부도/상장폐지 경고",
    },
    {
        "keywords": ["수주", "공급계약", "납품계약"],
        "min_amount": 10_000_000_000,   # 100억
        "large_cap_only": False,
        "priority": 2,
        "emoji": "🏆", "label": "대규모 수주/계약",
    },
    {
        "keywords": ["잠정실적", "실적발표", "영업이익"],
        "min_amount": 0,
        "large_cap_only": False,
        "priority": 3,
        "emoji": "📈", "label": "실적 발표",
    },
    {
        "keywords": ["자기주식취득", "자기주식 취득", "자사주매입"],
        "min_amount": 0,
        "large_cap_only": False,
        "priority": 4,
        "emoji": "💰", "label": "자사주 매입",
    },
    {
        "keywords": ["유상증자결정"],
        "min_amount": 0,
        "large_cap_only": True,   # 대형주(5000억+)만
        "priority": 5,
        "emoji": "📢", "label": "유상증자",
    },
]


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB), exist_ok=True)
    return sqlite3.connect(_DB)


def _ensure_table():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS dart_sent_alerts (
                rcept_no   TEXT PRIMARY KEY,
                corp_name  TEXT,
                report_nm  TEXT,
                date       TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)


def _is_sent(rcept_no: str) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM dart_sent_alerts WHERE rcept_no=?", (rcept_no,)
        ).fetchone()
    return row is not None


def _mark_sent(rcept_no: str, corp_name: str, report_nm: str, date: str):
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO dart_sent_alerts "
            "(rcept_no, corp_name, report_nm, date) VALUES (?, ?, ?, ?)",
            (rcept_no, corp_name, report_nm, date),
        )


def _count_today_sent(date: str) -> int:
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM dart_sent_alerts WHERE date=?", (date,)
            ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


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
    """공시 제목에서 금액(원) 추출"""
    m = re.search(r"([\d,]+)\s*억", title)
    if m:
        return int(m.group(1).replace(",", "")) * 100_000_000
    m = re.search(r"([\d,]+)\s*원", title)
    if m:
        return int(m.group(1).replace(",", ""))
    return 0


def _get_market_cap(kis, stock_code: str) -> int:
    """KIS API로 시가총액(억원) 조회. 실패 시 0 반환."""
    try:
        data = kis.get_stock_price(stock_code)
        return data.get("market_cap_억", 0)
    except Exception as e:
        logger.debug("시가총액 조회 실패 (%s): %s", stock_code, e)
        return 0


def check_and_alert() -> int:
    """새 공시 체크 및 알림 발송. 발송 건수 반환."""
    _ensure_table()
    today = datetime.now(_KST).strftime("%Y-%m-%d")

    # 오늘 발송 한도 확인
    already_sent = _count_today_sent(today)
    remaining = _DAILY_LIMIT - already_sent
    if remaining <= 0:
        logger.info("[DART] 오늘 발송 한도(%d건) 도달 — 스킵", _DAILY_LIMIT)
        return 0

    disclosures = fetch_today_disclosures()

    # KIS 클라이언트 초기화 (시가총액 조회용, KIS_APP_KEY 없으면 생략)
    kis = None
    if KIS_APP_KEY:
        try:
            from clients.kis_client import KISClient
            kis = KISClient()
        except Exception as e:
            logger.debug("KIS 클라이언트 초기화 실패: %s", e)

    # 알림 후보 수집
    matched: list[dict] = []

    for item in disclosures:
        rcept_no   = item.get("rcept_no", "")
        corp_name  = item.get("corp_name", "")
        report_nm  = item.get("report_nm", "")
        stock_code = (item.get("stock_code") or "").strip()
        corp_cls   = item.get("corp_cls", "")
        rcept_dt   = item.get("rcept_dt", "")

        # 이미 발송한 공시 스킵 (dart_sent_alerts 기준)
        if not rcept_no or _is_sent(rcept_no):
            continue

        # 코스피/코스닥 상장사만
        if corp_cls not in _LISTED_CLS:
            continue

        # 주식 코드 없으면 스킵
        if not stock_code:
            continue

        # 알림 규칙 매칭 (priority 순, 첫 매칭만 사용)
        matched_rule = None
        for rule in sorted(_ALERT_RULES, key=lambda r: r["priority"]):
            if not _matches_rule(report_nm, rule):
                continue
            amount = _extract_amount(report_nm)
            if rule["min_amount"] > 0 and amount < rule["min_amount"]:
                continue
            matched_rule = rule
            break

        if not matched_rule:
            continue

        # 시가총액 조회 (KIS 사용 가능 시)
        market_cap = _get_market_cap(kis, stock_code) if kis else 0

        # 소형주 제외 (KIS 사용 환경만 적용 — KIS 없으면 상장 여부로만 판단)
        if kis and market_cap < _MIN_MARKET_CAP_억:
            logger.debug("소형주 제외: %s(%s) 시총 %d억", corp_name, stock_code, market_cap)
            continue

        # 유상증자: 대형주(5000억+)만
        if matched_rule.get("large_cap_only") and kis and market_cap < _LARGE_CAP_억:
            logger.debug("유상증자 소형주 제외: %s 시총 %d억", corp_name, market_cap)
            continue

        matched.append({
            "rcept_no":   rcept_no,
            "corp_name":  corp_name,
            "report_nm":  report_nm,
            "stock_code": stock_code,
            "rcept_dt":   rcept_dt,
            "market_cap": market_cap,
            "rule":       matched_rule,
        })

    # 중요도 순 정렬, 일일 한도 적용
    matched.sort(key=lambda x: x["rule"]["priority"])
    to_send = matched[:remaining]

    if len(matched) > len(to_send):
        logger.info("[DART] 매칭 %d건 → 한도(%d건)로 %d건만 발송",
                    len(matched), _DAILY_LIMIT, len(to_send))

    sent = 0
    for item in to_send:
        rule     = item["rule"]
        rcept_dt = item["rcept_dt"]
        cap_str  = f" | 시총 {item['market_cap']:,}억" if item["market_cap"] else ""
        msg = (
            f"{rule['emoji']} *DART 공시 알림 [{rule['label']}]*\n"
            f"기업: {item['corp_name']}{cap_str}\n"
            f"공시: {item['report_nm']}\n"
            f"접수: {rcept_dt[:4]}-{rcept_dt[4:6]}-{rcept_dt[6:8]} "
            f"{rcept_dt[8:10]}:{rcept_dt[10:12]}\n"
            f"[DART 바로가기](https://dart.fss.or.kr/dsaf001/main.do?rcpNo={item['rcept_no']})"
        )
        try:
            send_message(msg)
            _mark_sent(item["rcept_no"], item["corp_name"], item["report_nm"], today)
            sent += 1
            logger.info("DART 알림 발송: %s - %s", item["corp_name"], item["report_nm"])
        except Exception as e:
            logger.warning("DART 알림 발송 실패: %s", e)

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
