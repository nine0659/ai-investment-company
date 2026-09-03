"""services/risk_gate.py 회귀 테스트 — CEO 자체 비중 밴드 위반 감지.

2026-09-03: TradingAgents(오픈소스 멀티에이전트 트레이딩 프레임워크) 리서치에서
Bull/Bear 토론 + 독립 Risk Manager 게이트 구조를 확인, 이 프로젝트엔 CEO 판단을
사후 검증하는 게이트가 decision_guard(데이터 불일치 전용)밖에 없었다는 공백을
메우기 위해 추가. LLM이 아니라 결정론적 코드로 검사 — 게이트 자신이 환각으로
오탐/누락하는 걸 막기 위함(헌장 원칙 2).
"""
from services.risk_gate import check_position_sizing


def _decisions(new_positions):
    return {"new_positions": new_positions}


def test_high_conviction_within_band_no_violation():
    d = _decisions([{"code": "005930", "name": "삼성전자", "conviction": "high", "size_pct": 7.0}])
    assert check_position_sizing(d) == []


def test_high_conviction_above_band_violation():
    d = _decisions([{"code": "005930", "name": "삼성전자", "conviction": "high", "size_pct": 12.0}])
    violations = check_position_sizing(d)
    assert len(violations) == 1
    assert "삼성전자" in violations[0] and "12.0" in violations[0]


def test_medium_conviction_below_band_violation():
    d = _decisions([{"code": "000660", "name": "SK하이닉스", "conviction": "medium", "size_pct": 2.0}])
    violations = check_position_sizing(d)
    assert len(violations) == 1
    assert "SK하이닉스" in violations[0]


def test_low_conviction_within_band_no_violation():
    d = _decisions([{"code": "035420", "name": "NAVER", "conviction": "low", "size_pct": 2.0}])
    assert check_position_sizing(d) == []


def test_unknown_conviction_skipped():
    d = _decisions([{"code": "035420", "name": "NAVER", "conviction": "", "size_pct": 99.0}])
    assert check_position_sizing(d) == []


def test_position_changes_ignored():
    """reduce/exit(position_changes)는 절대비중이 없어 밴드 위반 판단 대상이 아니다."""
    d = {"new_positions": [], "position_changes": [
        {"action": "reduce", "code": "005930", "name": "삼성전자", "size_change_pct": 50.0},
    ]}
    assert check_position_sizing(d) == []


def test_empty_decisions_no_violation():
    assert check_position_sizing({}) == []
    assert check_position_sizing(None) == []


def test_multiple_violations_all_reported():
    d = _decisions([
        {"code": "005930", "name": "삼성전자", "conviction": "high", "size_pct": 12.0},
        {"code": "000660", "name": "SK하이닉스", "conviction": "low", "size_pct": 5.0},
    ])
    assert len(check_position_sizing(d)) == 2
