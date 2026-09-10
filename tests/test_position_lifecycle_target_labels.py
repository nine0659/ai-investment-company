"""services/position_lifecycle_service.py 목표가 라벨 혼동 회귀 테스트 (2026-09-10).

evaluate_stage_transitions()의 "목표가 X%"(현재가/목표가 원값 비율)와
get_lifecycle_context()의 "목표가 X% 도달"(진입가→목표가 구간 진행률)이
같은 라벨로 서로 다른 값을 표시해 같은 종목에 대해 다른 화면에서 다른
숫자가 나올 수 있었다. 라벨을 "가격/목표가"와 "목표가 구간진행"으로
구분했다.
"""
from db.database import get_conn, init_db
from sqlalchemy import text

import services.position_lifecycle_service as position_lifecycle_service

_CODE = "005930"


def setup_function(_):
    init_db()
    with get_conn() as conn:
        conn.execute(text("DELETE FROM portfolio_positions WHERE code=:c"), {"c": _CODE})
        conn.execute(
            text("""
                INSERT INTO portfolio_positions
                (code, name, quantity, avg_price, target_price, status, thesis_stage)
                VALUES (:code, '삼성전자', 10, 50000, 100000, 'holding', 'developing')
            """),
            {"code": _CODE},
        )


def test_stage_transition_alert_uses_price_over_target_label():
    # 현재가 70000: 수익률(70000-50000)/50000=40% (>=25% developing→mature 기준 충족)
    # 가격/목표가 = 70000/100000 = 70%
    alerts = position_lifecycle_service.evaluate_stage_transitions(
        prices={_CODE: 70000}, auto_update=False
    )
    assert any("가격/목표가 70%" in a for a in alerts), alerts


def test_lifecycle_context_uses_range_progress_label():
    # 구간진행 = (70000-50000)/(100000-50000) = 40%
    ctx = position_lifecycle_service.get_lifecycle_context(prices={_CODE: 70000})
    assert "목표가 구간진행 40%" in ctx, ctx
    # 두 함수의 라벨이 실제로 다른 문자열이어야 한다 — 혼동 방지 확인
    assert "목표가 70%" not in ctx
