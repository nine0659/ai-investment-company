"""알림 피로 관리 — 오늘이 '결정할 거리가 있는 날'인지 코드가 먼저 판정한다.

매번 같은 길이로 말하면 정말 중요한 날의 경고가 묻힌다. 전일 지수 급변·보유 종목 큰 변동·
손절/목표 근접·고위험 이벤트가 있으면 '전체', 아니면 '간략'(🧭 섹션 2줄 이내)로 지시한다.
판정 이유는 항상 함께 주입해 브리핑이 왜 길거나 짧은지 사용자가 알 수 있게 한다.
"""
from datetime import date

KOSPI_FULL_PCT = 2.0
HOLDING_MOVE_PCT = 5.0
STOP_PCT = -12.0           # 평단 대비 손절(-15%) 근접 경고선


def decide(kospi_chg: float | None, holding_moves: dict[str, float] | None = None,
           holding_pl: dict[str, float] | None = None, events: list[dict] | None = None) -> dict:
    """반환: {mode: 'full'|'short', reasons: [...]}"""
    reasons = []
    if kospi_chg is not None and abs(kospi_chg) >= KOSPI_FULL_PCT:
        reasons.append(f"전일 KOSPI {kospi_chg:+.1f}%")
    for name, mv in (holding_moves or {}).items():
        if abs(mv) >= HOLDING_MOVE_PCT:
            reasons.append(f"{name} 하루 {mv:+.1f}%")
    for name, pl in (holding_pl or {}).items():
        if pl <= STOP_PCT:
            reasons.append(f"{name} 평단 대비 {pl:+.1f}% (손절 검토선 -15% 근접/초과)")
    for e in events or []:
        if e.get("impact") == "HIGH" and e.get("d_day", 99) <= 2:
            reasons.append(f"{e['title']} D-{e['d_day']}")
    return {"mode": "full" if reasons else "short", "reasons": reasons}


def context_line(d: dict) -> str:
    if d["mode"] == "full":
        return "[오늘 브리핑 모드: 전체 — 사유: " + "; ".join(d["reasons"]) + ". 🧭 섹션을 충실히 채울 것]"
    return "[오늘 브리핑 모드: 간략 — 결정을 요하는 변화 없음. 🧭 섹션은 2줄 이내(내 의견+확신도, 틀릴 조건)로, 반복 설명 금지]"
