"""평일 17:40 '오늘의 발굴·공시 요약' — 코드만으로 만든 1통 (LLM 없음 → 환각 위험·비용 0).

내용: ① 내 보유 종목 관련 공시 ② 어제 대비 새로 신호가 켜진 한국·미국 종목 ③ 오늘 시장 공시 하이라이트.
스냅샷·공시 사건은 평일 16:50~17:30에 무발송 잡이 쌓은 것이고, 수치는 전부 저장된 값에서 코드가 계산한다.
빈 날(보고할 게 없음)은 보내지 않고, 오늘분 스냅샷이 한국·미국 모두 없으면(수집 장애) 조용히 넘어가지 않고 fail로 기록한다.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")
_WEEK = "월화수목금토일"
_MAX_PER_SECTION = 5
_FIRST_DAY_TOP = 3        # 비교할 어제 스냅샷이 없으면 상위 몇 개만 보여준다 (신규라고 단정하지 않는다)


def _pct(a, b):
    return (a / b - 1) * 100 if a and b else None


def _kr_line(p: dict, ev: list[dict]) -> str:
    up, dd = _pct(p["target"], p["close"]), _pct(p["close"], p["high52"])
    stats = [s for s in (f"목표가 대비 {up:+.0f}%" if up is not None else "",
                         f"52주 고점 대비 {dd:.0f}%" if dd is not None else "") if s]
    line = f" · {p['name']}({p['code']}) {p['close']:,.0f}원 · 시총 {p['mcap_eok']:,.0f}억 · 신호 {p['score']}묶음 [{p['signals']}]"
    if stats:
        line += " " + " · ".join(stats)
    for e in [e for e in ev if e["polarity"] == "호재"][:1]:
        line += f"\n     └ 공시: {e['event_type']} {e['summary'] or ''} ({e['date']})".rstrip()
    for e in [e for e in ev if e["polarity"] == "악재"][:1]:
        line += f"\n     └ ⚠악재 공시: {e['event_type']} ({e['date']})"
    return line


def _us_line(p: dict) -> str:
    up, dd = _pct(p["target"], p["close"]), _pct(p["close"], p["high52"])
    stats = [s for s in (f"선행PER {p['fwd_pe']:.1f}배" if p["fwd_pe"] else "",
                         f"ROE {p['roe'] * 100:.0f}%" if p["roe"] is not None else "",
                         f"목표가 대비 {up:+.0f}%" if up is not None else "",
                         f"52주 고점 대비 {dd:.0f}%" if dd is not None else "") if s]
    return (f" · {p['name']}({p['code']}) ${p['close']:,.2f} · 시총 ${p['mcap_usd'] / 1e9:,.1f}B · {p['sector']} · "
            f"신호 {p['score']}묶음 [{p['signals']}]" + (" " + " · ".join(stats) if stats else ""))


_NEWS_TOP = 3          # 시장당 헤드라인을 붙이는 후보 수 (메시지 길이 때문에 상위 몇 개만)


def _with_news(lines: list[str], names: list[str], market: str) -> list[str]:
    """후보 줄 아래에 최근 뉴스 헤드라인을 붙인다(상위 _NEWS_TOP개). 수집 실패는 조용히 건너뛴다 — 필수 요소가 아니다."""
    out = []
    for i, (line, name) in enumerate(zip(lines, names)):
        out.append(line)
        if i < _NEWS_TOP:
            try:
                from services.candidate_news import lines_for
                out += lines_for(name, market)
            except Exception as e:
                logger.debug("[일일요약] 뉴스 실패(%s): %s", name, e)
    return out


def holdings_events(codes: set[str], days: int = 2) -> list[str]:
    from services.disclosure_events import recent_events
    out = []
    for e in recent_events(days=days, codes=codes):
        if e["event_type"] == "위험해소·절차":
            continue
        tag = {"호재": "호재", "악재": "⚠악재"}.get(e["polarity"], "참고")
        out.append(f" · {e['corp_name']}({e['stock_code']}) [{tag}] {e['event_type']}"
                   f"{(' — ' + e['summary']) if e['summary'] else ''} ({e['date']})")
    return out


def build(today: str | None = None, holding_codes: set[str] | None = None, force_scorecard: bool = False) -> dict:
    """{text: str|None, kr_ok, us_ok, counts:{...}}. text가 None이면 보낼 내용이 없는 것."""
    from services import market_ideas as mi
    from services.disclosure_events import by_code
    today = today or datetime.now(_KST).strftime("%Y-%m-%d")
    holding_codes = holding_codes or set()

    kr_date, kr_all, kr_prev = mi.kr_picks(set(), n=60)
    us_date, us_all, us_prev = mi.us_picks(set(), n=60)
    kr_ok, us_ok = kr_date == today, us_date == today
    sections: list[str] = []
    counts = {"holdings": 0, "kr_new": 0, "us_new": 0, "events": 0, "scorecard": 0}

    hold = holdings_events(holding_codes) if holding_codes else []
    if hold:
        counts["holdings"] = len(hold)
        sections.append("⚠ 내 보유 종목 공시 (최근 2일)\n" + "\n".join(hold))

    if kr_ok and kr_all:
        evs = by_code(days=7)
        mine = [p for p in kr_all if p["code"] not in holding_codes]
        if kr_prev:
            new = [p for p in mine if p["code"] not in kr_prev][:_MAX_PER_SECTION]
            head = "🇰🇷 어제 대비 새로 신호가 켜진 종목"
        else:
            new = mine[:_FIRST_DAY_TOP]
            head = "🇰🇷 오늘의 상위 후보 (비교할 어제 기록이 아직 없어 '신규' 표시는 내일부터)"
        if new:
            counts["kr_new"] = len(new)
            sections.append(head + "\n" + "\n".join(_with_news(
                [_kr_line(p, evs.get(p["code"], [])) for p in new], [p["name"] for p in new], "KR")))
    elif not kr_ok:
        sections.append("🇰🇷 오늘분 한국 스크리닝 없음 (수집 지연 — 내일 헬스체크 확인)")

    if us_ok and us_all:
        mine = [p for p in us_all if p["code"] not in holding_codes]
        if us_prev:
            new = [p for p in mine if p["code"] not in us_prev][:_MAX_PER_SECTION]
            head = "🇺🇸 어제 대비 새로 신호가 켜진 종목"
        else:
            new = mine[:_FIRST_DAY_TOP]
            head = "🇺🇸 오늘의 상위 후보 (비교할 어제 기록이 아직 없어 '신규' 표시는 내일부터)"
        if new:
            counts["us_new"] = len(new)
            sections.append(head + "\n" + "\n".join(_with_news(
                [_us_line(p) for p in new], [p["name"] for p in new], "US")))
    elif not us_ok:
        sections.append("🇺🇸 오늘분 미국 스크리닝 없음 (수집 지연 — 내일 헬스체크 확인)")

    try:
        ve = mi.validated_events(days=3)
    except Exception as e:
        logger.warning("[일일요약] 검증 통과 공시 실패 (건너뜀): %s", e)
        ve = []
    if ve:
        counts["validated"] = len(ve)
        sections.append("✔ 과거 검증을 통과한 공시 — 자사주 취득 결정 (최근 3일, 시총 1,000억+)\n"
                        "  (한국 2년 검증, 금융 제외: 공시 다음 거래일부터 20일 뒤 전 종목 중앙값 대비 +1.7%p, 이긴 시점 67% — 방향성 참고)\n"
                        + "\n".join(ve))
    try:
        hl = mi.event_highlights(days=1, n=6, skip_types=mi._VALIDATED_GOOD)
    except Exception as e:
        logger.warning("[일일요약] 공시 하이라이트 실패 (건너뜀): %s", e)
        hl = []
    if hl:
        counts["events"] = len(hl)
        sections.append("📰 오늘 시장 공시 하이라이트 (시총 3,000억+, 본문 숫자는 코드가 추출)\n"
                        "  (실적 발표는 과거 검증에서 발표 후 20~60일 시장 대비 오히려 부진한 경향이 있어 '호재'로 단정하지 않습니다)\n"
                        + "\n".join(hl))

    try:
        from services.industry_board import format_board, load_snapshots
        snaps = load_snapshots()
        if snaps and snaps[0][0] == today:                 # 오늘분 업종 온도계일 때만 (오래된 건 '오늘의' 요약에 쓰지 않는다)
            board = format_board(snaps)
            if board:
                counts["board"] = 1
                sections.append(board)
    except Exception as e:
        logger.warning("[일일요약] 업종 온도계 실패 (건너뜀): %s", e)

    # 금요일엔 스크리너 성적표를 덧붙인다 — 후보가 실제로 시장을 이겼는지(관측이 쌓인 뒤부터 나온다)
    if datetime.now(_KST).weekday() == 4 or force_scorecard:
        try:
            from services.screen_scorecard import format_report
            card = format_report()
            if card:
                counts["scorecard"] = 1
                sections.append(card)
        except Exception as e:
            logger.warning("[일일요약] 성적표 실패 (건너뜀): %s", e)

    informative = (counts["holdings"] + counts["kr_new"] + counts["us_new"] + counts["events"]
                   + counts.get("board", 0) + counts.get("validated", 0))
    if not informative:
        return {"text": None, "kr_ok": kr_ok, "us_ok": us_ok, "counts": counts}
    now = datetime.now(_KST)
    header = (f"📌 오늘의 발굴·공시 요약 ({now.strftime('%m/%d')} {_WEEK[now.weekday()]})\n"
              "코드 계산 · LLM 미사용 · 매수 권고가 아닌 '볼 만한 후보' 목록입니다. 📰는 기사 제목 원문이며 코드가 판단하지 않았습니다.\n"
              "※ ✔ 표시 신호만 과거 검증을 통과했습니다(한국 비금융 저PER·업종 대비 저평가(PER·PBR)·자사주 취득 공시 — 약 1.5~2년 한국 시장 기준이라 시기 특유의 "
              "효과일 수 있고, 금융주 랠리를 뺀 저PER 근거는 약함). 표시 없는 신호와 미국 신호는 시장 초과수익을 낸다는 검증은 아직 없습니다(가격 추세 신호는 효과가 없어 점수에서 뺐고, "
              "금요일 성적표가 후보의 실제 성과를 쌓아 검증합니다). 후보 탐색용이지 예측이 아닙니다.")
    return {"text": header + "\n\n" + "\n\n".join(sections), "kr_ok": kr_ok, "us_ok": us_ok, "counts": counts}


def snapshots_ready(today: str | None = None) -> tuple[bool, bool]:
    """(한국 오늘분 있음, 미국 오늘분 있음). 조회 실패는 '없음'으로 본다."""
    from db.database import get_conn
    from sqlalchemy import text
    today = today or datetime.now(_KST).strftime("%Y-%m-%d")
    out = []
    for table in ("universe_screen_daily", "us_screen_daily"):
        try:
            with get_conn() as conn:
                out.append(conn.execute(text(f"SELECT max(date) FROM {table}")).scalar() == today)
        except Exception:
            out.append(False)
    return out[0], out[1]


def wait_for_snapshots(max_wait: int = 600, poll: int = 60, sleep_fn=None, ready_fn=None) -> tuple[bool, bool]:
    """미국 스크리너는 약 14분 걸려 느린 날엔 17:40에 아직 도는 중일 수 있다 → 둘 다 올 때까지 최대 max_wait초 기다린다.
    끝내 안 오면 현재 상태를 반환하고, build()가 '오늘분 ... 없음'을 명시한다(조용히 빼지 않는다)."""
    import time
    sleep_fn = sleep_fn or time.sleep
    ready_fn = ready_fn or snapshots_ready
    waited = 0
    state = ready_fn()
    while not all(state) and waited < max_wait:
        sleep_fn(poll)
        waited += poll
        state = ready_fn()
    return state


def run_daily_job(send_fn=None, holding_codes: set[str] | None = None, wait: bool = False) -> tuple[str, str, str | None]:
    """(status, detail, 발송한 본문). 오늘분 스냅샷이 둘 다 없으면 fail, 보낼 내용이 없으면 success('무발송')."""
    if holding_codes is None:
        try:
            from services.portfolio_service import get_portfolio
            holding_codes = {p["code"] for p in get_portfolio()}
        except Exception as e:
            logger.warning("[일일요약] 보유 종목 조회 실패 (보유 공시 섹션 없이 진행): %s", e)
            holding_codes = set()
    if wait:
        wait_for_snapshots()
    res = build(holding_codes=holding_codes)
    if not res["kr_ok"] and not res["us_ok"]:
        return "fail", "오늘분 한국·미국 스냅샷이 모두 없음 (스크리너 잡 확인 필요)", None
    if res["text"] is None:
        return "success", "보고할 내용 없음 — 무발송", None
    if send_fn is None:
        from clients.telegram_client import send_message as send_fn
    ok = send_fn(res["text"])
    c = res["counts"]
    detail = (f"보유공시 {c['holdings']} · 한국 {c['kr_new']} · 미국 {c['us_new']} · 하이라이트 {c['events']}"
              + ("" if res["kr_ok"] and res["us_ok"] else " · ⚠일부 스냅샷 없음"))
    if not ok:
        return "fail", "텔레그램 발송 실패 — " + detail, None
    return "success", detail, res["text"]
