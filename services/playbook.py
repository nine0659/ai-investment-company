"""국면별 행동 플레이북 — 평온할 때 미리 정해둔 규칙을 급변장에 그대로 꺼내 쓴다.

급락 당일의 즉석 판단이 가장 비싼 실수를 낳는다. 국면(전일 KOSPI 등락)을 코드가 분류하고,
해당 국면의 사전 규칙을 CEO 컨텍스트에 주입해 "규칙에 비추어 지금은 이렇다"고 말하게 한다.
기본안은 가치투자자 일반 원칙이며 사용자가 /playbook 으로 수정·확정한다 — 확정 전에는
출력에 '기본안(미확정)'이라고 표시한다.
"""
import json
import logging

from sqlalchemy import text

from db.database import get_conn

logger = logging.getLogger(__name__)

REGIMES = ("shock_down", "down", "calm", "up", "shock_up")
LABEL = {"shock_down": "급락(전일 -3% 이하)", "down": "약세(-1~-3%)", "calm": "평온(±1%)",
         "up": "강세(+1~+3%)", "shock_up": "급등(전일 +3% 이상)"}

DEFAULT_RULES = {
    "shock_down": [
        "당일 시장가 공황매도 금지 — 매도는 미리 정한 반증 신호(종가선·평단 -15%)에 걸린 종목만",
        "하루에 계좌 전체의 10% 넘는 비중 변경 금지(분할 실행)",
        "현금이 목표 미만이면 신규 매수 대기, 목표 이상이면 관심 종목을 3회 분할 검토",
        "장 시작 30분은 판단 보류(호가·변동 과대) — 장중 확인 후 결정",
    ],
    "down": ["새 매도 결정은 반증 신호가 있을 때만", "신규 매수는 평소 비중 한도 안에서만"],
    "calm": ["기존 계획 유지 — 변화 신호 없으면 매매하지 않는 것도 결정"],
    "up": ["급등 추격 매수 금지 — 계획한 비중 안에서만", "목표가 근접 종목은 분할 차익 실현 검토"],
    "shock_up": [
        "추격 매수 금지 — 오른 이유(실적/수급/테마)를 확인하기 전에는 비중 확대하지 않음",
        "목표가·비중 한도를 넘은 종목은 초과분 분할 정리 검토",
    ],
}


def classify(kospi_chg: float | None) -> str:
    if kospi_chg is None:
        return "calm"
    if kospi_chg <= -3:
        return "shock_down"
    if kospi_chg <= -1:
        return "down"
    if kospi_chg >= 3:
        return "shock_up"
    if kospi_chg >= 1:
        return "up"
    return "calm"


def _key(regime: str) -> str:
    return f"playbook.{regime}"


def get_rules(regime: str) -> tuple[list[str], bool]:
    """(규칙 목록, 사용자 확정 여부)"""
    try:
        with get_conn() as conn:
            row = conn.execute(text("SELECT value FROM system_settings WHERE key=:k"),
                               {"k": _key(regime)}).fetchone()
        if row:
            return json.loads(row[0]), True
    except Exception as e:
        logger.debug("[플레이북] 조회 실패: %s", e)
    return DEFAULT_RULES[regime], False


def set_rules(regime: str, rules: list[str]) -> bool:
    if regime not in REGIMES or not rules:
        return False
    try:
        with get_conn() as conn:
            conn.execute(text("DELETE FROM system_settings WHERE key=:k"), {"k": _key(regime)})
            conn.execute(text("INSERT INTO system_settings (key, value) VALUES (:k, :v)"),
                         {"k": _key(regime), "v": json.dumps(rules, ensure_ascii=False)})
        return True
    except Exception as e:
        logger.warning("[플레이북] 저장 실패: %s", e)
        return False


def context_block(kospi_chg: float | None) -> str:
    regime = classify(kospi_chg)
    rules, confirmed = get_rules(regime)
    tag = "사용자 확정" if confirmed else "기본안(미확정 — 사용자가 /playbook 으로 수정 가능)"
    head = f"[국면 플레이북 — 현재 국면: {LABEL[regime]} · {tag}. 브리핑은 이 규칙에 비추어 판단하고, 규칙과 다른 제안을 할 땐 이유를 밝힐 것]"
    return head + "\n" + "\n".join(f"  {i}. {r}" for i, r in enumerate(rules, 1))


def format_all() -> str:
    out = []
    for r in REGIMES:
        rules, confirmed = get_rules(r)
        out.append(f"*{LABEL[r]}* ({'확정' if confirmed else '기본안'})\n" + "\n".join(f"  {i}. {x}" for i, x in enumerate(rules, 1)))
    return "\n\n".join(out)
