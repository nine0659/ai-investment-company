"""드로다운 정책 + NAV 데이터가드 회귀 테스트.

2026-07-08 사고: KIS 시세 부분 누락으로 NAV 총평가가 5,134만→3,052만으로 왜곡
기록됐고, 90일 최대값 대비 드로다운이 -44.4%로 오판되어 실계좌 전량 청산이
자동 실행됐다 (보유목록이 우연히 빈 값으로 와서 실제 주문은 0건).
→ 정책 변경(사용자 승인): 드로다운은 경보만, 자동매도 금지.
"""
import os

from services.nav_service import _nav_data_suspicious

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_scheduler_never_auto_liquidates():
    """스케줄러가 드로다운으로 자동매도를 재도입하면 실패해야 한다 (사용자 승인 필요)."""
    with open(os.path.join(_ROOT, "scheduler.py"), encoding="utf-8") as f:
        src = f.read()
    assert "execute_drawdown_defense" not in src, (
        "scheduler.py가 드로다운 자동매도를 호출함 — 2026-07-09 정책 위반. "
        "드로다운은 경보만 발송한다 (2026-07-08 전량청산 오판 사고)."
    )


def test_guard_detects_missing_price():
    pnl = [
        {"name": "삼성전자", "invested": 10_000_000, "current_val": 11_000_000},
        {"name": "고장난종목", "invested": 5_000_000, "current_val": 0},
    ]
    reason = _nav_data_suspicious(pnl, prev_total=50_000_000)
    assert "시세 누락" in reason and "고장난종목" in reason


def test_guard_detects_value_crash():
    # 2026-07-08 실제 수치: 직전 51,343,500 → 오늘 30,523,000 (-40.6%)
    pnl = [{"name": "A", "invested": 27_500_000, "current_val": 30_523_000}]
    reason = _nav_data_suspicious(pnl, prev_total=51_343_500)
    assert "총평가 급변" in reason


def test_guard_passes_normal_day():
    pnl = [{"name": "A", "invested": 27_500_000, "current_val": 50_500_000}]
    assert _nav_data_suspicious(pnl, prev_total=51_343_500) == ""


def test_guard_passes_first_record():
    pnl = [{"name": "A", "invested": 27_500_000, "current_val": 30_000_000}]
    assert _nav_data_suspicious(pnl, prev_total=None) == ""
