"""CEO 스스로가 코드에 명문화한 포지션 사이징 규칙(확신도별 비중 밴드)을
CEO 자신이 어겼는지 결정론적으로 검사한다.

`services/decision_guard.py`와는 성격이 다르다 — decision_guard는 "데이터 자체가
의심스러운" 상황에서 발송을 보류시키는 회로차단기지만, 이 모듈은 "데이터는 멀쩡한데
CEO의 판단이 스스로 정한 규칙과 어긋난" 상황을 감지한다. 판단 자체엔 CEO 나름의 이유가
있을 수 있으므로 발송을 막지 않고 경고만 덧붙인다 — 헌장의 "의심스러우면 조용히
밀어붙이지 않고 사람에게 알린다"는 철학은 같지만 강도는 다르다(2026-09-03, 사용자 확인).

또 다른 LLM 게이트를 추가하지 않는 이유: 헌장 원칙 2("계산은 코드가, LLM은 서술만")를
게이트 자체에도 적용한 것 — LLM 게이트는 그 자신이 환각으로 오탐/누락할 수 있다.
"""
import logging

logger = logging.getLogger(__name__)

# agents/ceo_agent.py [포트폴리오 운용 원칙]에 이미 명문화된 확신도별 비중 밴드.
_BANDS = {"high": (5.0, 10.0), "medium": (3.0, 5.0), "low": (1.0, 3.0)}


def check_position_sizing(ceo_decisions: dict) -> list[str]:
    """새 포지션 제안이 확신도별 비중 밴드를 벗어났는지 검사.

    reduce/exit(position_changes)는 절대 비중이 아니라 변화폭(size_change_pct)만
    갖고 있어 밴드 위반 여부를 애매하지 않게 판단할 수 없으므로 v1 범위에서 제외.
    포트폴리오 전체 집중도 체크는 향후 확장 지점(portfolio_service의 현재 비중과
    교차해야 하므로 별도 설계 필요).

    반환: 위반 설명 문자열 목록. 위반 없으면 빈 리스트.
    """
    violations: list[str] = []
    for pos in (ceo_decisions or {}).get("new_positions", []) or []:
        conviction = pos.get("conviction", "")
        band = _BANDS.get(conviction)
        size = pos.get("size_pct")
        if band is None or size is None:
            continue
        if not (band[0] <= size <= band[1]):
            violations.append(
                f"{pos.get('name', '')}({pos.get('code', '')}) "
                f"확신도 {conviction} — 비중 {size}%가 권고 범위 "
                f"{band[0]}~{band[1]}% 밖"
            )
    if violations:
        logger.warning("[리스크게이트] 비중 밴드 위반 %d건: %s", len(violations), violations)
    return violations
