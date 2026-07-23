"""고신뢰도 포지션 변경 권고 전 교차검증 브레이크.

2026-07-22 KOSPI 실시간 오염 사고: 잘못 고정된 유효범위 때문에 실제로는
없었던 -7.18% 급락이 브리핑에 실렸고, CEO는 그 수치를 근거로 삼성전기·
현대차 비중 50% 축소를 권고해 그대로 발송됐다(같은 날 버그 수정 후
재실행에서야 정정). 당시 수치 자체는 서킷브레이커 범위(±10%) 안에 들어서
기존 이상치 필터를 통과했다 — 절대범위 검사만으로는 "그럴듯하게 틀린 값"을
못 잡는다.

이 모듈은 그 대신 같은 실행에서 수집된 서로 다른 한국 시장 대리지표
(KOSPI 실시간 vs EWY 한국 ETF)끼리 방향이 크게 어긋나는지를 보고,
그런 상태에서 큰 폭의 포지션 변경/청산/고비중 신규편입 같은 "고신뢰도 액션"이
함께 나왔을 때만 발송을 보류시킨다. 자동매도 정책과 동일한 철학 —
의심스러우면 조용히 밀어붙이지 않고 사람에게 알린다.
"""
import logging

logger = logging.getLogger(__name__)

# 이 이상 비중 변경/청산은 '고신뢰도 액션'으로 간주해 교차검증 대상에 올린다.
HIGH_CONVICTION_CHANGE_PCT = 20.0
# 신규편입도 이 비중 이상 + high 확신이면 같은 취급.
HIGH_CONVICTION_NEW_SIZE_PCT = 5.0
# 두 대리지표가 이 이상 벌어지고 부호까지 반대면 '불일치'로 본다.
MIN_DIVERGENCE_MAGNITUDE = 3.0


def high_conviction_actions(decisions: dict) -> list[str]:
    """ceo_decisions에서 고신뢰도 액션을 사람이 읽을 문자열 목록으로 반환. 없으면 []."""
    flags: list[str] = []
    for chg in (decisions or {}).get("position_changes", []):
        name = chg.get("name", ""); code = chg.get("code", "")
        if chg.get("action") == "exit":
            flags.append(f"{name}({code}) 전량청산")
        elif abs(chg.get("size_change_pct", 0) or 0) >= HIGH_CONVICTION_CHANGE_PCT:
            flags.append(f"{name}({code}) {chg.get('action')} {chg.get('size_change_pct')}%p")
    for pos in (decisions or {}).get("new_positions", []):
        if (pos.get("size_pct", 0) or 0) >= HIGH_CONVICTION_NEW_SIZE_PCT and pos.get("conviction") == "high":
            flags.append(f"{pos.get('name','')}({pos.get('code','')}) 신규 고비중 {pos.get('size_pct')}%")
    return flags


def market_data_inconsistencies(raw_market_data: dict, kr_index_realtime: dict) -> list[str]:
    """같은 실행에서 수집된 한국 시장 대리지표끼리 방향 불일치를 사람이 읽을 문자열 목록으로 반환."""
    warnings: list[str] = []
    kospi = ((kr_index_realtime or {}).get("kospi") or {}).get("change_pct")
    ewy = ((raw_market_data or {}).get("ewy") or {}).get("change_pct")
    if kospi is not None and ewy is not None:
        same_sign = (kospi > 0) == (ewy > 0)
        if not same_sign and abs(kospi) >= MIN_DIVERGENCE_MAGNITUDE and abs(ewy) >= MIN_DIVERGENCE_MAGNITUDE:
            warnings.append(f"KOSPI {kospi:+.2f}% vs EWY(한국ETF) {ewy:+.2f}% 방향 불일치")
    return warnings


def gate_high_conviction_actions(
    ceo_decisions: dict, raw_market_data: dict, kr_index_realtime: dict,
) -> dict:
    """고신뢰도 액션 + 데이터 불일치가 동시에 있으면 발송 보류 판정.

    반환: {"blocked": bool, "reason": str, "actions": [...], "inconsistencies": [...]}
    """
    actions = high_conviction_actions(ceo_decisions)
    if not actions:
        return {"blocked": False, "reason": "", "actions": [], "inconsistencies": []}

    inconsistencies = market_data_inconsistencies(raw_market_data, kr_index_realtime)
    if not inconsistencies:
        return {"blocked": False, "reason": "", "actions": actions, "inconsistencies": []}

    reason = (
        "고신뢰도 액션(" + "; ".join(actions) + ") 발견 시점에 "
        "데이터 불일치(" + "; ".join(inconsistencies) + ") — 발송 보류"
    )
    logger.warning("[의사결정가드] %s", reason)
    return {"blocked": True, "reason": reason, "actions": actions, "inconsistencies": inconsistencies}
