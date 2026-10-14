"""확신도 보정 — "확신도 상"이 실제로 얼마나 맞았는가, 그리고 기준선을 이기는가.

AI가 쓰는 확신도(상/중/하)가 실제 적중률과 연결돼 있지 않았다. decision_scoring이 채점한 판단을
확신도별로 모아 적중률을 보여주고, 표본이 모자라거나 기준선(동전 던지기 50%)을 못 이기면
"확신도를 말하지 말고 조건부 의견만 내라"는 지시를 컨텍스트에 붙인다. (과신 억제 장치)
"""
MIN_N = 8          # 확신도 구간별 최소 표본
BASELINE = 50.0    # 방향 판단의 동전 던지기 기준선(%)


def stats(results: list[dict], horizon: int = 5) -> dict:
    """확신도별 {n, hit%}. 확신도 미기록 판단은 'none'."""
    out: dict[str, dict] = {}
    for r in results:
        if r.get("horizon") != horizon:
            continue
        k = r.get("conviction") or "none"
        g = out.setdefault(k, {"n": 0, "ok": 0})
        g["n"] += 1
        g["ok"] += 1 if r.get("correct") else 0
    for g in out.values():
        g["hit"] = g["ok"] / g["n"] * 100 if g["n"] else 0.0
    return out


def verdict(st: dict) -> dict:
    """{reliable: 확신도 해석 가능 여부, overconfident: 확신도 상이 중보다 낮거나 기준선 이하, notes: [...]}"""
    notes = []
    hi, mid, lo = st.get("high"), st.get("medium"), st.get("low")
    total = sum(g["n"] for g in st.values())
    reliable = bool(hi and hi["n"] >= MIN_N)
    over = False
    if hi and hi["n"] >= MIN_N and hi["hit"] <= BASELINE:
        over = True
        notes.append(f"확신도 '상' 판단 {hi['n']}건의 적중 {hi['hit']:.0f}% — 기준선({BASELINE:.0f}%) 이하: 과신")
    if hi and mid and hi["n"] >= MIN_N and mid["n"] >= MIN_N and hi["hit"] < mid["hit"]:
        over = True
        notes.append(f"확신도 '상'({hi['hit']:.0f}%)이 '중'({mid['hit']:.0f}%)보다 낮음 — 확신도가 실력과 반대로 움직임")
    if not reliable:
        notes.append(f"확신도별 표본이 부족(전체 {total}건, '상' {hi['n'] if hi else 0}건) — 확신도의 신뢰도를 판단할 수 없음")
    return {"reliable": reliable, "overconfident": over, "notes": notes}


def format_for_prompt(results: list[dict]) -> str:
    st = stats(results)
    if not st:
        return ""
    v = verdict(st)
    lines = ["[확신도 보정 — 과거 판단 5거래일 후 채점, 코드 계산]"]
    for k, label in (("high", "상"), ("medium", "중"), ("low", "하")):
        g = st.get(k)
        if g:
            lines.append(f"  확신도 {label}: {g['n']}건 중 {g['ok']}건 적중 ({g['hit']:.0f}%)")
    lines += [f"  ※ {n}" for n in v["notes"]]
    if v["overconfident"] or not v["reliable"]:
        lines.append("  → 지시: '확신도 상'을 남발하지 말고, 근거가 약하면 확신도 '하'와 '조건부 의견'으로 쓸 것. "
                     "확신도는 위 실제 적중률을 넘지 않게 표현할 것")
    return "\n".join(lines)
