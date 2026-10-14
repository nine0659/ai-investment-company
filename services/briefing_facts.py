"""브리핑의 핵심 수치를 LLM이 아니라 코드가 확정해 박아 넣는 모듈 (절대원칙 2번).

2026-10-14 사고: 같은 브리핑 안에서 ① 전일 KOSPI -5.46% 폭락이 아예 언급되지 않았고
② "S&P500선물 +0.12%"가 실제(-0.09%)와 달랐으며 ③ "원달러 강세(+0.46%)"처럼 환율이
오른 것을(=원화 약세) 반대로 서술했다. 팩트 시트는 정확히 주어졌는데도 LLM이 옮겨 적다
틀렸다 — 그래서 '수치' 줄과 시장 요약 블록은 코드가 만들어 LLM 서술을 덮어쓴다.
LLM 서술 중 방향이 명백히 반대인 표현은 교정한다(삭제가 아니라 올바른 표현으로 치환).
"""
import logging
import re

logger = logging.getLogger(__name__)

SHOCK_PCT = 3.0   # 전일 지수 등락이 이 이상이면 별도 경고 줄


def _chg(raw: dict, key: str, field: str = "change_pct"):
    v = ((raw or {}).get(key) or {}).get(field)
    return v if isinstance(v, (int, float)) and v == v else None


def _fut_pct(raw: dict, key: str):
    """선물은 오버나잇(realtime_pct)을 우선, 없으면 일봉 등락."""
    d = (raw or {}).get(key) or {}
    for f in ("realtime_pct", "change_pct"):
        v = d.get(f)
        if isinstance(v, (int, float)) and v == v:
            return v
    return None


def fx_phrase(pct: float) -> str:
    """달러/원이 오르면 원화 약세. 반대로 쓰는 실수를 코드로 차단."""
    if pct > 0.1:
        return "원화 약세"
    if pct < -0.1:
        return "원화 강세"
    return "원화 보합"


def build_numbers_line(raw: dict, kr_rt: dict) -> str:
    parts = []
    k = ((kr_rt or {}).get("kospi") or {})
    if k.get("change_pct") is not None:
        parts.append(f"KOSPI(전일 마감) {k['current']:,.0f} ({k['change_pct']:+.2f}%)")
    sp = _fut_pct(raw, "sp500_futures")
    if sp is not None:
        parts.append(f"S&P500선물 {sp:+.2f}%")
    nq = _fut_pct(raw, "nasdaq_futures")
    if nq is not None:
        parts.append(f"나스닥선물 {nq:+.2f}%")
    fx = (raw or {}).get("usd_krw") or {}
    if isinstance(fx.get("close"), (int, float)):
        c = fx.get("change_pct")
        tail = f" ({c:+.2f}%, {fx_phrase(c)})" if isinstance(c, (int, float)) else ""
        parts.append(f"달러/원 {fx['close']:,.0f}원{tail}")
    vix = _chg(raw, "vix", "close")
    if vix is not None:
        parts.append(f"VIX {vix:.2f}")
    ewy = _chg(raw, "ewy")
    if ewy is not None:
        parts.append(f"EWY {ewy:+.2f}%")
    return "수치(코드 확정): " + " · ".join(parts) if parts else ""


def build_market_block(raw: dict, kr_rt: dict, run_type: str) -> str:
    """브리핑 맨 위에 붙는 확정 수치 블록. 전일 지수 급변은 별도 경고."""
    label = "전일 마감" if run_type == "pre_market" else "오늘"
    lines = []
    for key, name in (("kospi", "KOSPI"), ("kosdaq", "KOSDAQ")):
        d = (kr_rt or {}).get(key) or {}
        if d.get("change_pct") is not None:
            lines.append(f"{name} {label} {d['current']:,.0f} ({d['change_pct']:+.2f}%)")
    if not lines:
        return ""
    block = "📊 확정 수치(코드 계산): " + " · ".join(lines)
    k = ((kr_rt or {}).get("kospi") or {}).get("change_pct")
    if k is not None and abs(k) >= SHOCK_PCT:
        block += f"\n🚨 {label} KOSPI {k:+.2f}% — 변동성 {'급락' if k < 0 else '급등'} 구간. 아래 판단은 이 변동을 전제로 읽을 것."
    return block


_FIXES = (
    # (정규식, 조건, 치환) — 환율 방향 표현이 실제와 반대일 때만 교정
    (re.compile(r"원\s?달러\s?(?:환율\s?)?강세|달러\s?강세"), "fx_down_wrong", "달러/원 상승(원화 약세)"),
)


def find_contradictions(report: str, raw: dict) -> list[str]:
    """LLM 서술이 코드 수치와 방향이 반대인 경우를 문자열 목록으로 반환."""
    out = []
    fx = _chg(raw, "usd_krw")
    if fx is not None:
        if fx > 0.1 and re.search(r"원화\s?강세", report):
            out.append(f"'원화 강세'라 썼지만 달러/원 {fx:+.2f}% (= 원화 약세)")
        if fx < -0.1 and re.search(r"원화\s?약세", report):
            out.append(f"'원화 약세'라 썼지만 달러/원 {fx:+.2f}% (= 원화 강세)")
        if fx > 0.1 and re.search(r"원\s?달러\s?(?:환율\s?)?강세", report):
            out.append(f"'원달러 강세'라 썼지만 달러/원 {fx:+.2f}% (원화는 약세)")
    sp, nq = _fut_pct(raw, "sp500_futures"), _fut_pct(raw, "nasdaq_futures")
    if sp is not None and nq is not None:
        if sp < 0 and nq < 0 and min(sp, nq) <= -0.1 and re.search(r"선물\s?(?:소폭\s?|강하게\s?)?(?:상승|강세)", report):
            out.append(f"'선물 상승'이라 썼지만 S&P선물 {sp:+.2f}%·나스닥선물 {nq:+.2f}%")
        if sp > 0 and nq > 0 and max(sp, nq) >= 0.1 and re.search(r"선물\s?(?:소폭\s?)?(?:하락|약세)", report):
            out.append(f"'선물 하락'이라 썼지만 S&P선물 {sp:+.2f}%·나스닥선물 {nq:+.2f}%")
    return out


def apply(report: str, raw: dict, kr_rt: dict, run_type: str) -> tuple[str, list[str]]:
    """수치 줄 덮어쓰기 + 확정 블록 삽입 + 모순 교정. (새 리포트, 발견된 모순)"""
    contradictions = find_contradictions(report, raw)
    text = report
    # 환율 방향 교정 (서술 삭제가 아니라 올바른 표현으로 치환)
    fx = _chg(raw, "usd_krw")
    if fx is not None and fx > 0.1:
        text = re.sub(r"원\s?달러\s?(?:환율\s?)?강세", "달러/원 상승(원화 약세)", text)
        text = re.sub(r"원화\s?강세", "원화 약세", text)
    elif fx is not None and fx < -0.1:
        text = re.sub(r"원화\s?약세", "원화 강세", text)

    line = build_numbers_line(raw, kr_rt)
    if line and run_type == "pre_market":
        if re.search(r"^수치\s?:.*$", text, flags=re.M):
            text = re.sub(r"^수치\s?:.*$", lambda _m: line, text, count=1, flags=re.M)
        else:
            text = text.rstrip() + "\n\n" + line
    block = build_market_block(raw, kr_rt, run_type)
    if block:
        text = block + "\n\n" + text
    if contradictions:
        text += "\n\n⚠️ 자동 교정: " + "; ".join(contradictions)
    return text, contradictions
