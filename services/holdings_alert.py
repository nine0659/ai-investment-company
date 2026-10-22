"""보유 종목 공시 즉시 알림 — 평일 장중 30분마다 새 공시를 훑어 *내 보유 종목*에 해당하는 것만 바로 보낸다.

17:40 일일 요약은 하루 한 번이라 장중에 뜬 유상증자·횡령 같은 악재는 몇 시간 늦는다. 이 잡은 새로 저장된 사건 중
보유 종목 것만 이벤트(접수번호) 단위로 한 번씩 알린다. 발송 중복은 claim_report_slot(date, 'hold_<접수번호>')로 막는다
(Render·수동 실행·재시작 복구가 겹쳐도 같은 공시는 한 번). 제목 분류는 코드 규칙이고 세부 조건은 원문을 보라고 명시한다.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")
_IGNORE_TYPES = {"위험해소·절차"}


def _format(e: dict) -> str:
    tag = {"호재": "📈 호재", "악재": "⚠️ 악재"}.get(e["polarity"], "📋 참고")
    body = f"\n{e['summary']}" if e.get("summary") else ""
    return (f"🔔 보유 종목 공시 — {e['corp_name']}({e['stock_code']})\n"
            f"{tag} · {e['event_type']}{body}\n"
            f"접수 {e['date']} · 원문: https://dart.fss.or.kr/dsaf001/main.do?rcpNo={e['rcept_no']}\n"
            "※ 제목을 코드 규칙으로 분류한 것입니다. 금액·조건 등 세부 내용은 원문에서 확인하세요.")


def alert_new_events(events: list[dict], holding_codes: set[str], send_fn=None, claim_fn=None, release_fn=None) -> int:
    """새로 저장된 사건 중 보유 종목 것을 사건당 1회 발송. 발송한 건수를 반환."""
    if send_fn is None:
        from clients.telegram_client import send_message as send_fn
    if claim_fn is None or release_fn is None:
        from services.report_service import claim_report_slot as claim_fn, release_report_slot as release_fn
    today = datetime.now(_KST).strftime("%Y-%m-%d")
    sent = 0
    for e in events:
        if e["stock_code"] not in holding_codes or e["event_type"] in _IGNORE_TYPES:
            continue
        slot = f"hold_{e['rcept_no']}"
        if not claim_fn(today, slot):
            continue
        try:
            ok = send_fn(_format(e))
        except Exception as ex:
            logger.error("[보유공시] 발송 예외: %s", ex)
            ok = False
        if ok:
            sent += 1
        else:
            release_fn(today, slot)                              # 다음 회차에 다시 시도
    return sent


def run_once(holding_codes: set[str] | None = None) -> dict:
    """공시 수집(최근 1일) → 새 사건 중 보유 종목만 알림. {sent, new, ok}."""
    if holding_codes is None:
        from services.portfolio_service import get_portfolio
        holding_codes = {p["code"] for p in get_portfolio()}
    if not holding_codes:
        return {"sent": 0, "new": 0, "ok": True, "skipped": "보유 종목 없음"}
    from services.disclosure_events import collect
    st = collect(days=1)
    if not st["ok"]:
        return {"sent": 0, "new": 0, "ok": False, "errors": st["errors"]}
    return {"sent": alert_new_events(st.get("events") or [], holding_codes), "new": st["new"], "ok": True}
