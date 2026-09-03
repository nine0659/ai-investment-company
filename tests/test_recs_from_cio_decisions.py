"""services/recommendation_service.py::recs_from_cio_decisions 회귀 테스트.

2026-09-03: 이 함수는 원래 브리핑 텍스트의 "목표 +X% / 손절 -Y%" 문구를 정규식으로
긁어왔으나, 2026-06-19 브리핑 포맷 개편(9줄 압축형) 이후 그 문구가 리포트에 더 이상
나타나지 않아 사실상 죽은 코드였다(어디서도 호출되지도 않았음). 텍스트 파싱 대신
코드로 직접 목표가/손절가를 계산하도록 재설계 — 진입가는 항상 실데이터, 없으면 폐기
(recs_from_weekly_picks의 환각 차단 원칙과 동일).
"""
from services.recommendation_service import recs_from_cio_decisions


def _decisions(new_positions):
    return {"new_positions": new_positions}


def test_entry_price_zero_discards_position():
    """price_fn이 0을 반환하면(조회 실패) 환각 방지를 위해 그 포지션은 폐기."""
    d = _decisions([{"code": "005930", "name": "삼성전자", "risk_reward": "3:1"}])
    recs = recs_from_cio_decisions(d, price_fn=lambda code: 0)
    assert recs == []


def test_stop_price_is_15pct_below_entry():
    d = _decisions([{"code": "005930", "name": "삼성전자", "risk_reward": "3:1"}])
    recs = recs_from_cio_decisions(d, price_fn=lambda code: 100_000)
    assert len(recs) == 1
    assert recs[0]["stop_price"] == 85_000
    assert recs[0]["entry_price"] == 100_000


def test_target_price_uses_parsed_risk_reward_ratio():
    d = _decisions([{"code": "005930", "name": "삼성전자", "risk_reward": "3:1"}])
    recs = recs_from_cio_decisions(d, price_fn=lambda code: 100_000)
    # stop 15,000원 하락폭 * 비율 3 = 45,000원 상승폭
    assert recs[0]["target_price"] == 145_000


def test_unparseable_risk_reward_uses_default_ratio():
    d = _decisions([{"code": "005930", "name": "삼성전자", "risk_reward": "확신도 높음"}])
    recs = recs_from_cio_decisions(d, price_fn=lambda code: 100_000)
    # 기본 비율 2.5 * 15,000 = 37,500
    assert recs[0]["target_price"] == 137_500


def test_missing_risk_reward_field_uses_default_ratio():
    d = _decisions([{"code": "005930", "name": "삼성전자"}])
    recs = recs_from_cio_decisions(d, price_fn=lambda code: 100_000)
    assert recs[0]["target_price"] == 137_500


def test_duplicate_code_skipped():
    d = _decisions([
        {"code": "005930", "name": "삼성전자", "risk_reward": "3:1"},
        {"code": "005930", "name": "삼성전자", "risk_reward": "3:1"},
    ])
    recs = recs_from_cio_decisions(d, price_fn=lambda code: 100_000)
    assert len(recs) == 1


def test_no_code_skipped():
    d = _decisions([{"code": "", "name": "이름만있음", "risk_reward": "3:1"}])
    recs = recs_from_cio_decisions(d, price_fn=lambda code: 100_000)
    assert recs == []


def test_empty_new_positions_returns_empty_list():
    assert recs_from_cio_decisions({}, price_fn=lambda code: 100_000) == []
    assert recs_from_cio_decisions({"new_positions": []}, price_fn=lambda code: 100_000) == []


def test_rationale_carries_thesis():
    d = _decisions([{"code": "005930", "name": "삼성전자", "risk_reward": "3:1", "thesis": "메모리 업사이클"}])
    recs = recs_from_cio_decisions(d, price_fn=lambda code: 100_000)
    assert recs[0]["rationale"] == "메모리 업사이클"
