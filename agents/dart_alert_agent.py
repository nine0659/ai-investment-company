"""
DART 공시 에이전트
- 브리핑 통합: fetch_for_briefing() → 오늘 중요 공시를 수집해 브리핑 컨텍스트에 포함
- 필터링: 코스피/코스닥 상장 대형주(시총 5000억+), B타입 주요사항 공시만
- 독립 알림(check_and_alert)은 레거시 유지 (현재 파이프라인에서 비활성화)
"""
import logging
import re
from datetime import datetime, time as _time
from zoneinfo import ZoneInfo

import requests

from config.settings import DART_API_KEY, KIS_APP_KEY
from clients.telegram_client import send_message
from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)

_KST  = ZoneInfo("Asia/Seoul")
_BASE = "https://opendart.fss.or.kr/api"

_DAILY_LIMIT = 5   # 하루 최대 5건 (기존 10건 → 축소)

# 발송 허용 시간 (KST) — 새벽·심야 무분별 발송 방지
_SEND_START = _time(7, 0)
_SEND_END   = _time(20, 0)

# DART corp_cls: Y=KOSPI, K=KOSDAQ 상장사만 대상
_LISTED_CLS = {"Y", "K"}

# 시가총액 기준 (억원) — 기존 1000억에서 5000억으로 상향
_MIN_MARKET_CAP_억 = 5_000   # 중형주 이상만 (코스피200·코스닥150 수준)
_LARGE_CAP_억      = 10_000  # 유상증자 전용 대형주 기준

# B타입(주요사항보고)만 — A타입(사업/분기보고서 등 정기공시)은 무분별 발송 원인이므로 제외
_PBLNTF_TYPES = ["B"]

# 알림 규칙 (priority 낮을수록 먼저 발송)
# ⚠️ 금액 기준(min_amount)은 공시 제목에서 파싱 — 제목에 금액 없는 경우 0으로 처리됨
_ALERT_RULES = [
    {
        "keywords": ["부도", "파산", "상장폐지", "관리종목지정", "영업정지"],
        "min_amount": 0,
        "large_cap_only": False,
        "priority": 1,
        "emoji": "🚨", "label": "부도/상장폐지 경고",
    },
    {
        # 수주: 500억 이상만 (기존 100억 → 대폭 상향)
        "keywords": ["수주", "공급계약", "납품계약"],
        "min_amount": 50_000_000_000,   # 500억
        "large_cap_only": False,
        "priority": 2,
        "emoji": "🏆", "label": "대규모 수주/계약(500억+)",
    },
    {
        # 잠정실적: 시총 5000억+ 대형주로 한정
        "keywords": ["잠정실적"],
        "min_amount": 0,
        "large_cap_only": False,   # 시총 기준(_MIN_MARKET_CAP_억)으로 이미 필터링
        "priority": 3,
        "emoji": "📈", "label": "잠정실적 발표",
    },
    {
        # 자사주 매입: 1000억+ 대형주만 (소형주 자사주는 주가부양 목적 노이즈 많음)
        "keywords": ["자기주식취득", "자기주식 취득", "자사주매입"],
        "min_amount": 0,
        "large_cap_only": True,    # 1만억 이상 대형주만
        "priority": 4,
        "emoji": "💰", "label": "자사주 매입",
    },
    {
        # 유상증자: 1만억 이상 초대형주만
        "keywords": ["유상증자결정"],
        "min_amount": 0,
        "large_cap_only": True,
        "priority": 5,
        "emoji": "📢", "label": "유상증자",
    },
]


def _is_sent(rcept_no: str) -> bool:
    try:
        with get_conn() as conn:
            row = conn.execute(
                text("SELECT 1 FROM dart_sent_alerts WHERE rcept_no=:no"),
                {"no": rcept_no},
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _mark_sent(rcept_no: str, corp_name: str, report_nm: str, date: str):
    try:
        with get_conn() as conn:
            conn.execute(
                text(
                    "INSERT INTO dart_sent_alerts (rcept_no, corp_name, report_nm, date) "
                    "VALUES (:no, :corp, :nm, :date) ON CONFLICT (rcept_no) DO NOTHING"
                ),
                {"no": rcept_no, "corp": corp_name, "nm": report_nm, "date": date},
            )
    except Exception:
        pass


def _count_today_sent(date: str) -> int:
    try:
        with get_conn() as conn:
            row = conn.execute(
                text("SELECT COUNT(*) FROM dart_sent_alerts WHERE date=:date"),
                {"date": date},
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
    now = datetime.now(_KST)

    # 발송 허용 시간대 체크 (07:00~20:00 KST)
    if not (_SEND_START <= now.time() <= _SEND_END):
        logger.debug("[DART] 발송 허용 시간 외 스킵 (%s KST)", now.strftime("%H:%M"))
        return 0

    pass  # init_db()에서 테이블 보장됨
    today = now.strftime("%Y-%m-%d")

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
            # amount==0: 제목에 금액 미기재(대부분의 계약 공시) → 금액 불명으로 간주, 통과
            if rule["min_amount"] > 0 and amount > 0 and amount < rule["min_amount"]:
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


def fetch_for_briefing() -> list[dict]:
    """브리핑 통합용: 오늘 중요 DART 공시 수집 (텔레그램 직접 발송 없음).

    - 시간대·일일 한도 제약 없음 (브리핑 파이프라인 내 항상 실행)
    - KIS 시총 조회는 시도하되 실패 시 생략 (시총 기준 필터 미적용)
    - 최대 10건 반환
    """
    pass  # init_db()에서 테이블 보장됨
    disclosures = fetch_today_disclosures()

    kis = None
    if KIS_APP_KEY:
        try:
            from clients.kis_client import KISClient
            kis = KISClient()
        except Exception:
            pass

    matched: list[dict] = []
    for item in disclosures:
        rcept_no   = item.get("rcept_no", "")
        corp_name  = item.get("corp_name", "")
        report_nm  = item.get("report_nm", "")
        stock_code = (item.get("stock_code") or "").strip()
        corp_cls   = item.get("corp_cls", "")

        if not rcept_no or not stock_code:
            continue
        if corp_cls not in _LISTED_CLS:
            continue

        matched_rule = None
        for rule in sorted(_ALERT_RULES, key=lambda r: r["priority"]):
            if not _matches_rule(report_nm, rule):
                continue
            amount = _extract_amount(report_nm)
            # amount==0: 제목에 금액 미기재 → 금액 불명으로 간주, 통과
            if rule["min_amount"] > 0 and amount > 0 and amount < rule["min_amount"]:
                continue
            matched_rule = rule
            break

        if not matched_rule:
            continue

        market_cap = _get_market_cap(kis, stock_code) if kis else 0
        if kis and market_cap > 0 and market_cap < _MIN_MARKET_CAP_억:
            continue
        if matched_rule.get("large_cap_only") and kis and market_cap > 0 and market_cap < _LARGE_CAP_억:
            continue

        matched.append({
            "corp_name":  corp_name,
            "report_nm":  report_nm,
            "stock_code": stock_code,
            "market_cap": market_cap,
            "rule":       matched_rule,
            "rcept_no":   rcept_no,
        })

    matched.sort(key=lambda x: x["rule"]["priority"])
    return matched[:10]


def format_disclosures_for_briefing(disclosures: list[dict]) -> str:
    """브리핑에 삽입할 DART 공시 텍스트 생성."""
    if not disclosures:
        return ""
    lines = []
    for d in disclosures:
        rule    = d["rule"]
        cap_str = f" (시총 {d['market_cap']:,}억)" if d.get("market_cap") else ""
        lines.append(f"  {rule['emoji']} {d['corp_name']}{cap_str}: {d['report_nm']}")
    return "📢 오늘 주요 DART 공시:\n" + "\n".join(lines)


def run():
    """독립 실행 진입점 (레거시 — 현재는 브리핑에 통합)"""
    now = datetime.now(_KST)
    logger.info("[DART] 시작 — %s KST", now.strftime("%H:%M"))
    items = fetch_for_briefing()
    logger.info("[DART] 완료 — %d건 탐지", len(items))
    for it in items:
        logger.info("  %s %s: %s", it["rule"]["emoji"], it["corp_name"], it["report_nm"])


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s %(message)s")
    run()
