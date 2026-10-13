"""복기(review_feedback_team)용 사실 계산 — 숫자는 코드가, LLM은 해석만.

기존 복기 프롬프트는 "예측 정확도 X/10점", "금요일 추천 성과 저조", "RSI 28 이하 평균 +5%"
같은 값을 LLM에게 직접 만들라고 했다. 입력에 없는 통계를 지어내기 쉬운 구조라(절대원칙 2)
요일별 성과·종목 쏠림·표본 크기를 여기서 계산해 주입하고, 표본이 작으면 '패턴 단정 금지'
문구를 코드가 붙인다.
"""
from collections import Counter
from datetime import datetime

_WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]
# 이 건수 미만이면 어떤 '패턴'도 우연과 구분되지 않는다.
MIN_SAMPLE_FOR_PATTERN = 8


def _weekday(date_str: str) -> str | None:
    try:
        return _WEEKDAY_KR[datetime.strptime(date_str[:10], "%Y-%m-%d").weekday()]
    except (ValueError, TypeError):
        return None


def build_review_facts(recs: list[dict]) -> str:
    """최근 추천 목록(return_pct 확정분)에서 요일별 성과·쏠림·표본 경고를 계산해 문자열로."""
    rets = [r for r in recs if r.get("return_pct") is not None]
    n = len(rets)
    lines = [f"[코드 계산 복기 팩트 — 이 숫자만 인용, 새 통계를 만들지 마라] 확정 추천 {n}건"]
    if n == 0:
        lines.append("  추천 성과 데이터 없음 → 패턴·승률을 언급하지 말 것")
        return "\n".join(lines)

    # 요일별 평균 (표본 1~2건짜리 요일은 평균이 의미 없어 건수만 표기)
    by_day: dict[str, list[float]] = {}
    for r in rets:
        d = _weekday(r.get("date", ""))
        if d:
            by_day.setdefault(d, []).append(float(r["return_pct"]))
    day_parts = []
    for d in _WEEKDAY_KR:
        v = by_day.get(d)
        if not v:
            continue
        day_parts.append(f"{d} {len(v)}건 평균 {sum(v) / len(v):+.1f}%" if len(v) >= 3 else f"{d} {len(v)}건(평균 산출 불가)")
    if day_parts:
        lines.append("  요일별: " + " | ".join(day_parts))

    # 종목 쏠림: 같은 종목이 반복 추천되는 정도
    cnt = Counter(r.get("name") or r.get("code") for r in rets)
    top_name, top_n = cnt.most_common(1)[0]
    lines.append(f"  종목 쏠림: 최다 추천 {top_name} {top_n}건 / 서로 다른 종목 {len(cnt)}개")

    lines.append(
        f"  표본 경고: {n}건뿐이라 '반복 패턴'이라고 단정하지 말고 '가능성'으로만 서술"
        if n < MIN_SAMPLE_FOR_PATTERN
        else f"  표본: {n}건 (패턴 서술 가능하되 요일별 3건 미만 구간은 제외)"
    )
    return "\n".join(lines)
