"""CEO가 포지션마다 적는 '반증 신호'(이게 발생하면 투자 근거 훼손)를 코드로 감시한다.

2026-10-13 진단: 운영 DB의 실제 반증 신호가 "수출 둔화", "성장성 불확실", "수요 회복 지연"처럼
측정이 불가능한 문장뿐이라 프롬프트가 요구하는 '반증'이 형식적 문구였다 — 쓰기만 하고 아무도
확인하지 않았다. 이 모듈은 ① 문장을 분류(코드 판정 가능 / 사람 판단 필요 / 모호) ② 코드 판정이
가능한 것(종목 가격 기준선, 평단 대비 %, KOSPI·환율·VIX·미국10년물 수준)은 현재값과 비교해
발동 여부를 계산한다. LLM을 쓰지 않는다(판정기가 환각하면 안 되므로).

판정 가능한 형식 예: "종가 85,000원 하회" / "평단 대비 -15%" / "KOSPI 7,000 이탈" /
"USD/KRW 1,450 상회" / "VIX 30 돌파" / "미 10Y 5% 상회".
"""
import logging
import re

logger = logging.getLogger(__name__)

_DOWN = r"(?:하회|이탈|붕괴|깨지|밑돌|미만|아래|하락\s*마감|하향\s*돌파)"
_UP = r"(?:상회|돌파|초과|넘|이상|상승\s*마감)"
_NUM = r"(\d[\d,]*(?:\.\d+)?)"

# (키, 라벨 패턴, 단위 패턴) — 시장 지표 수준 트리거. 키는 raw_market_data의 키와 같다.
_MARKET_KEYS = [
    ("kospi", r"(?:KOSPI|코스피)", ""),
    ("kosdaq", r"(?:KOSDAQ|코스닥)", ""),
    ("usd_krw", r"(?:환율|USD/KRW|원/달러|달러/원)", ""),
    ("vix", r"VIX", ""),
    ("us10y", r"(?:미\s*10Y|미국\s*10Y|10년물|미\s*10년|미국채\s*10년)", r"\s*%?"),
]

# 모호함 판별: 수치도 기준선도 없는 서술
_HAS_NUMBER = re.compile(r"\d")
_VAGUE_WORDS = re.compile(r"둔화|불확실|악화|지연|리스크|우려|약화|부진|하락세|둔화세")

# 이 건수 미만이면 '모호' 판정의 신뢰가 낮다 — 너무 짧은 빈 문자열은 제외
_MIN_LEN = 2


def _f(s: str) -> float:
    return float(s.replace(",", ""))


def parse_triggers(text: str) -> list[dict]:
    """반증 신호 문장에서 코드 판정 가능한 트리거를 추출.

    반환 원소: {"kind": "stock_price"|"vs_avg"|"market", "key": str, "op": "<"|">", "value": float, "raw": str}
    """
    out: list[dict] = []
    if not text:
        return out

    # 시장 지표 수준 (지표명 근처의 숫자 + 방향어)
    for key, label, unit in _MARKET_KEYS:
        for m in re.finditer(label + r"[^\d\n]{0,8}" + _NUM + unit + r"\s*(?:선|포인트|p)?\s*(" + _DOWN + "|" + _UP + ")", text, re.I):
            op = "<" if re.fullmatch(_DOWN, m.group(2)) or re.search(_DOWN, m.group(2)) else ">"
            out.append({"kind": "market", "key": key, "op": op, "value": _f(m.group(1)), "raw": m.group(0).strip()})

    # 평단 대비 %: "평단 대비 -15%", "매수가 대비 15% 하락"
    for m in re.finditer(r"(?:평단|평균단가|매수가|매입가)\s*(?:대비)?\s*([+\-−]?\d+(?:\.\d+)?)\s*%\s*(" + _DOWN + "|하락|손실)?", text):
        v = _f(m.group(1).replace("−", "-"))
        if m.group(2) or v < 0:
            out.append({"kind": "vs_avg", "key": "avg", "op": "<", "value": -abs(v), "raw": m.group(0).strip()})

    # 종목 가격 기준선: "85,000원 하회" (시장지표 문구에 이미 걸린 숫자는 제외)
    taken = {t["value"] for t in out}
    for m in re.finditer(_NUM + r"\s*원\s*(?:선)?\s*(" + _DOWN + "|" + _UP + ")", text):
        v = _f(m.group(1))
        if v in taken:
            continue
        op = "<" if re.search(_DOWN, m.group(2)) else ">"
        out.append({"kind": "stock_price", "key": "price", "op": op, "value": v, "raw": m.group(0).strip()})
    return out


def classify(text: str) -> str:
    """'coded'(코드 판정 가능) | 'manual'(수치 있으나 코드 판정 불가 → CEO 재판단) | 'vague'(측정 불가)."""
    t = (text or "").strip()
    if len(t) < _MIN_LEN:
        return "vague"
    if parse_triggers(t):
        return "coded"
    if _HAS_NUMBER.search(t):
        return "manual"
    return "vague"


def evaluate(text: str, price: float | None, avg_price: float | None, market: dict | None) -> dict:
    """현재값과 비교해 발동된 트리거 목록을 계산.

    반환: {"status": classify, "fired": [raw...], "unchecked": [raw...]}
    비교 대상 값이 없으면(조회 실패 등) 발동 아님이 아니라 unchecked로 분리한다 — 침묵 금지.
    """
    status = classify(text)
    fired: list[str] = []
    unchecked: list[str] = []
    market = market or {}
    for t in parse_triggers(text):
        cur = None
        if t["kind"] == "stock_price":
            cur = price
        elif t["kind"] == "vs_avg":
            cur = ((price - avg_price) / avg_price * 100) if price and avg_price else None
        elif t["kind"] == "market":
            v = (market.get(t["key"]) or {}).get("close")
            cur = float(v) if isinstance(v, (int, float)) else None
        if cur is None:
            unchecked.append(t["raw"])
            continue
        if (t["op"] == "<" and cur < t["value"]) or (t["op"] == ">" and cur > t["value"]):
            fired.append(f'{t["raw"]} (현재 {cur:,.2f})')
    return {"status": status, "fired": fired, "unchecked": unchecked}


def _load_holdings_with_falsification() -> list[dict]:
    from db.database import get_conn
    from sqlalchemy import text
    with get_conn() as conn:
        rows = conn.execute(text(
            "SELECT code, name, avg_price, falsification FROM portfolio_positions "
            "WHERE status='holding' AND quantity > 0")).fetchall()
    return [{"code": r[0], "name": r[1], "avg_price": r[2], "falsification": r[3] or ""} for r in rows]


def build_status_context(prices: dict[str, float], market: dict | None) -> str:
    """CEO 프롬프트용 '반증 조건 점검' 블록 — 코드가 판정한 결과만 담는다. 보유가 없거나 실패하면 ''."""
    try:
        holdings = _load_holdings_with_falsification()
    except Exception as e:
        logger.debug("[반증감시] 보유 조회 실패: %s", e)
        return ""
    if not holdings:
        return ""
    lines = ["[보유종목 반증 조건 점검 — 코드 판정. 발동된 것은 오늘 결정에 반드시 반영하라]"]
    vague_names: list[str] = []
    for h in holdings:
        res = evaluate(h["falsification"], prices.get(h["code"]), h["avg_price"], market)
        if not h["falsification"]:
            vague_names.append(h["name"])
            continue
        if res["fired"]:
            lines.append(f"  🚨 {h['name']}({h['code']}) 반증 발동: " + "; ".join(res["fired"]) + f" ← 기록: {h['falsification'][:60]}")
            try:
                from services.falsification_probe import recent_for
                why = recent_for(h["code"])
                if why:
                    lines.append(f"     └ AI 조사(가설·검증 전, 확인할 방향일 뿐 근거 아님): {why}")
            except Exception as e:
                logger.debug("[반증감시] 조사 결과 조회 실패: %s", e)
        elif res["status"] == "coded":
            tail = f" (현재값 조회 불가 {len(res['unchecked'])}건)" if res["unchecked"] else ""
            lines.append(f"  ✅ {h['name']}({h['code']}) 반증 미발동{tail}: {h['falsification'][:60]}")
        elif res["status"] == "manual":
            lines.append(f"  ❔ {h['name']}({h['code']}) 수치 조건이나 코드 판정 불가 — CIO가 직접 확인: {h['falsification'][:60]}")
        else:
            vague_names.append(h["name"])
    if vague_names:
        lines.append(
            "  ⚠️ 측정 불가능한 반증 조건(모호/미기록): " + ", ".join(vague_names)
            + " → 이번 hold 줄에서 '지표+기준값' 형태의 측정 가능한 조건으로 다시 쓸 것"
        )
    return "\n".join(lines) if len(lines) > 1 else ""
