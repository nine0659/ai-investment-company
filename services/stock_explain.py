"""종목별 '왜 후보인가' 설명 — 전 종목 스크리닝 스냅샷·공시 사건에 저장된 값만 보여주는 코드 출력 (LLM 없음).

봇의 /why 명령용. 한국(6자리 코드/종목명)은 universe_screen_daily + dart_events, 미국(영문 티커)은 us_screen_daily.
스냅샷에 없는 종목(시총 기준 미달·ETF·우선주 등)은 없다고 솔직히 말한다 — 값을 지어내지 않는다.
"""
import logging
import re

logger = logging.getLogger(__name__)

_SIGNAL_HELP = {
    "수익성": "자기자본 대비 이익이 높음", "저평가": "추정PER이 낮음", "자산대비저평가": "PBR 1배 미만",
    "업종대비저평가": "업종 중앙값보다 PER이 크게 낮음", "이익증가기대": "추정이익이 현재보다 20%+ 증가",
    "컨센서스여력": "애널리스트 평균 목표가 대비 여력 20%+", "고점대비낙폭": "52주 고점 대비 30%+ 하락",
    "쌍끌이매수": "최근 3일 중 외국인·기관이 모두 2일+ 순매수", "공시호재": "최근 호재성 공시",
    "상대강도": "3개월 수익률이 전 종목 상위 30%", "추세회복": "큰 낙폭 후 50일선 위로 회복",
    "단기회복": "큰 낙폭 후 50일선 위로 회복", "내부자매수": "임원·이사가 최근 공개시장에서 매수",
}


def _q(sql: str, params: dict):
    from db.database import get_conn
    from sqlalchemy import text
    with get_conn() as conn:
        return conn.execute(text(sql), params).fetchall()


def _pct(a, b):
    return (a / b - 1) * 100 if a and b else None


def _signal_lines(signals: str) -> list[str]:
    return [f"   · {k}: {_SIGNAL_HELP.get(k, '')}".rstrip(": ") for k in (signals or "").split(",") if k]


def _history(table: str, code: str) -> str:
    rows = _q(f"SELECT date, score FROM {table} WHERE code=:c ORDER BY date DESC LIMIT 6", {"c": code})
    if len(rows) < 2:
        return ""
    return "   최근 신호 묶음 수: " + " ← ".join(f"{d[5:]} {s}" for d, s in rows)


def explain_kr(code: str) -> str:
    rows = _q("SELECT date, name, market, close, chg_pct, mcap_eok, per, pbr, cns_per, eps, target, high52, score, signals "
              "FROM universe_screen_daily WHERE code=:c ORDER BY date DESC LIMIT 1", {"c": code})
    if not rows:
        return (f"{code}: 한국 전 종목 스크리닝 기록에 없습니다. (시총 1,000억 미만, ETF·우선주·스팩, 상세 조회 실패, "
                "또는 스크리너가 아직 돌지 않은 경우)")
    date, name, market, close, chg, mcap, per, pbr, cns_per, eps, target, high52, score, signals = rows[0]
    up, dd = _pct(target, close), _pct(close, high52)
    lines = [f"🔎 {name}({code}) [{market}] — {date} 스크리닝 기준",
             f"   {close:,.0f}원 · 시총 {mcap:,.0f}억 · 신호 {score}묶음" + (" (3묶음 이상 = 후보)" if score >= 3 else " (3묶음 미만 = 후보 아님)")]
    stats = [s for s in (f"추정PER {cns_per:.1f}배" if cns_per else "", f"PBR {pbr:.2f}배" if pbr else "",
                         f"목표가 대비 {up:+.0f}%" if up is not None else "",
                         f"52주 고점 대비 {dd:.0f}%" if dd is not None else "") if s]
    if stats:
        lines.append("   " + " · ".join(stats))
    sig = _signal_lines(signals)
    lines += ["   켜진 신호:"] + sig if sig else ["   켜진 신호 없음"]
    h = _history("universe_screen_daily", code)
    if h:
        lines.append(h)
    try:
        from services.disclosure_events import by_code
        evs = by_code(days=14).get(code, [])
        if evs:
            lines.append("   최근 2주 공시:")
            for e in evs[:5]:
                tag = {"호재": "호재", "악재": "⚠악재"}.get(e["polarity"], "참고")
                lines.append(f"   · ({e['date']}) [{tag}] {e['event_type']}" + (f" — {e['summary']}" if e["summary"] else ""))
    except Exception as e:
        logger.warning("[why] 공시 조회 실패: %s", e)
    lines.append("※ 코드가 저장된 값으로 계산한 설명이며 매수 권고가 아닙니다.")
    return "\n".join(lines)


def explain_us(ticker: str) -> str:
    rows = _q("SELECT date, name, sector, close, chg_pct, mcap_usd, fwd_pe, roe, target, high52, score, signals "
              "FROM us_screen_daily WHERE code=:c ORDER BY date DESC LIMIT 1", {"c": ticker})
    if not rows:
        return (f"{ticker}: 미국 전 종목 스크리닝 기록에 없습니다. (시총 $30억 미만 또는 상위 1,700종목 밖, ETF, 시세 수신 실패, "
                "또는 스크리너가 아직 돌지 않은 경우)")
    date, name, sector, close, chg, mcap, fpe, roe, target, high52, score, signals = rows[0]
    up, dd = _pct(target, close), _pct(close, high52)
    lines = [f"🔎 {name}({ticker}) [{sector}] — {date} 스크리닝 기준",
             f"   ${close:,.2f} · 시총 ${mcap / 1e9:,.1f}B · 신호 {score}묶음" + (" (3묶음 이상 = 후보)" if score >= 3 else " (3묶음 미만 = 후보 아님)")]
    stats = [s for s in (f"선행PER {fpe:.1f}배" if fpe else "", f"ROE {roe * 100:.0f}%" if roe is not None else "",
                         f"목표가 대비 {up:+.0f}%" if up is not None else "",
                         f"52주 고점 대비 {dd:.0f}%" if dd is not None else "") if s]
    if stats:
        lines.append("   " + " · ".join(stats))
    sig = _signal_lines(signals)
    lines += ["   켜진 신호:"] + sig if sig else ["   켜진 신호 없음"]
    h = _history("us_screen_daily", ticker)
    if h:
        lines.append(h)
    lines.append("※ 코드가 저장된 값으로 계산한 설명이며 매수 권고가 아닙니다. 재무는 최근 완결 사업연도(SEC) 기준입니다.")
    return "\n".join(lines)


def explain(query: str) -> str:
    """6자리 코드·한국 종목명·영문 티커를 받아 설명. 못 알아보면 사용법을 돌려준다."""
    q = (query or "").strip()
    if not q:
        return "사용법: `/why 종목명|코드|티커` (예: `/why 삼성전자`, `/why 005930`, `/why NVDA`)"
    try:
        if re.fullmatch(r"[0-9A-Za-z]{6}", q) and any(c.isdigit() for c in q):
            return explain_kr(q.upper())
        if re.fullmatch(r"[A-Za-z][A-Za-z.\-]{0,5}", q):
            return explain_us(q.upper().replace(".", "-"))
        from services.research_service import resolve_code
        code, name = resolve_code(q)
        if code:
            return explain_kr(code)
        return f"'{q}' 종목을 찾지 못했습니다. 종목명 대신 6자리 코드나 영문 티커로 입력해 보세요."
    except Exception as e:
        logger.warning("[why] 실패: %s", e)
        return "조회 중 오류가 났습니다. 잠시 후 다시 시도해 주세요."
