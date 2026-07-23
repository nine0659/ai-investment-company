"""고신뢰도 액션 발송 전 교차검증 브레이크 회귀 테스트.

2026-07-22 사고 재현: KOSPI 실시간이 오염돼 -7.18%로 나왔지만 서킷브레이커
범위(±10%) 안이라 기존 이상치 필터를 통과했고, CEO는 그걸 근거로 삼성전기·
현대차 비중 50% 축소를 권고해 그대로 발송됐다. 이 가드는 그 상황(고신뢰도
액션 + 대리지표 간 방향 불일치)을 잡아 발송을 보류시켜야 한다.
"""
from services.decision_guard import (
    gate_high_conviction_actions,
    high_conviction_actions,
    market_data_inconsistencies,
)


def _decisions(**overrides):
    base = {"position_changes": [], "new_positions": []}
    base.update(overrides)
    return base


def test_no_decisions_no_flags():
    assert high_conviction_actions({}) == []
    assert high_conviction_actions(_decisions()) == []


def test_large_reduce_is_high_conviction():
    d = _decisions(position_changes=[
        {"action": "reduce", "code": "009150", "name": "삼성전기", "size_change_pct": 50},
    ])
    flags = high_conviction_actions(d)
    assert len(flags) == 1
    assert "삼성전기" in flags[0]


def test_exit_is_always_high_conviction_regardless_of_pct():
    d = _decisions(position_changes=[
        {"action": "exit", "code": "005380", "name": "현대차", "size_change_pct": 0},
    ])
    assert len(high_conviction_actions(d)) == 1


def test_small_reduce_is_not_high_conviction():
    d = _decisions(position_changes=[
        {"action": "reduce", "code": "005930", "name": "삼성전자", "size_change_pct": 5},
    ])
    assert high_conviction_actions(d) == []


def test_high_conviction_new_position_flagged():
    d = _decisions(new_positions=[
        {"code": "005930", "name": "삼성전자", "size_pct": 7, "conviction": "high"},
    ])
    assert len(high_conviction_actions(d)) == 1


def test_low_conviction_new_position_not_flagged():
    d = _decisions(new_positions=[
        {"code": "005930", "name": "삼성전자", "size_pct": 7, "conviction": "medium"},
    ])
    assert high_conviction_actions(d) == []


def test_kospi_ewy_opposite_direction_flagged():
    """2026-07-22 사고 재현: KOSPI -7.18% vs EWY +6.16% (실제로는 KOSPI도 상승이었음)."""
    warnings = market_data_inconsistencies(
        raw_market_data={"ewy": {"change_pct": 6.16}},
        kr_index_realtime={"kospi": {"change_pct": -7.18}},
    )
    assert len(warnings) == 1
    assert "불일치" in warnings[0]


def test_kospi_ewy_same_direction_not_flagged():
    warnings = market_data_inconsistencies(
        raw_market_data={"ewy": {"change_pct": 5.0}},
        kr_index_realtime={"kospi": {"change_pct": 5.3}},
    )
    assert warnings == []


def test_small_noise_divergence_not_flagged():
    """둘 다 부호는 반대여도 변동폭이 미미하면(노이즈 수준) 불일치로 안 봄."""
    warnings = market_data_inconsistencies(
        raw_market_data={"ewy": {"change_pct": -0.4}},
        kr_index_realtime={"kospi": {"change_pct": 0.3}},
    )
    assert warnings == []


def test_missing_data_not_flagged():
    assert market_data_inconsistencies({}, {}) == []
    assert market_data_inconsistencies({"ewy": {"change_pct": 5.0}}, {}) == []


def test_gate_blocks_when_high_conviction_action_and_inconsistency_coincide():
    """2026-07-22 사고 재현 전체 시나리오 — 반드시 blocked=True."""
    decisions = _decisions(position_changes=[
        {"action": "reduce", "code": "009150", "name": "삼성전기", "size_change_pct": 50},
        {"action": "reduce", "code": "005380", "name": "현대차", "size_change_pct": 50},
    ])
    result = gate_high_conviction_actions(
        decisions,
        raw_market_data={"ewy": {"change_pct": 6.16}},
        kr_index_realtime={"kospi": {"change_pct": -7.18}},
    )
    assert result["blocked"] is True
    assert "삼성전기" in result["reason"]
    assert "불일치" in result["reason"]


def test_gate_allows_high_conviction_action_when_data_consistent():
    """액션이 커도 데이터가 서로 맞으면 정상 발송 (과잉 차단 방지)."""
    decisions = _decisions(position_changes=[
        {"action": "reduce", "code": "009150", "name": "삼성전기", "size_change_pct": 50},
    ])
    result = gate_high_conviction_actions(
        decisions,
        raw_market_data={"ewy": {"change_pct": -6.0}},
        kr_index_realtime={"kospi": {"change_pct": -5.5}},
    )
    assert result["blocked"] is False


def test_gate_allows_inconsistent_data_when_no_high_conviction_action():
    """데이터가 어긋나도 액션 자체가 작으면(일상적 브리핑) 차단하지 않음 — 과잉 차단 방지."""
    decisions = _decisions(position_changes=[
        {"action": "reduce", "code": "005930", "name": "삼성전자", "size_change_pct": 3},
    ])
    result = gate_high_conviction_actions(
        decisions,
        raw_market_data={"ewy": {"change_pct": 6.16}},
        kr_index_realtime={"kospi": {"change_pct": -7.18}},
    )
    assert result["blocked"] is False
