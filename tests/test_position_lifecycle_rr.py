"""CIO 신규 진입 R/R 검증 회귀 테스트."""

from services.position_lifecycle_service import check_rr_warnings


def test_check_rr_warnings_accepts_valid_ratio():
    decisions = {"new_positions": [
        {"code": "005930", "name": "삼성전자", "risk_reward": "3.2:1"},
    ]}
    assert check_rr_warnings(decisions) == []


def test_check_rr_warnings_flags_missing_ratio():
    decisions = {"new_positions": [
        {"code": "005930", "name": "삼성전자", "risk_reward": ""},
    ]}
    warnings = check_rr_warnings(decisions)
    assert warnings and "미명시" in warnings[0]


def test_check_rr_warnings_flags_unparseable_ratio():
    decisions = {"new_positions": [
        {"code": "005930", "name": "삼성전자", "risk_reward": "확신도 높음"},
    ]}
    warnings = check_rr_warnings(decisions)
    assert warnings and "형식 오류" in warnings[0]


def test_check_rr_warnings_flags_below_minimum_ratio():
    decisions = {"new_positions": [
        {"code": "005930", "name": "삼성전자", "risk_reward": "2.5:1"},
    ]}
    warnings = check_rr_warnings(decisions)
    assert warnings and "미달" in warnings[0]
