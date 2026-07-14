"""드로다운 정책 + NAV 데이터가드 회귀 테스트.

2026-07-08 사고: 90일 NAV 고점 대비 드로다운이 -44.4%로 오판되어 실계좌
전량 청산이 자동 실행됐다 (보유목록이 우연히 빈 값으로 와서 실제 주문은 0건).
→ 정책 변경(사용자 승인): 드로다운은 경보만, 자동매도 금지.

2026-07-10 진단 정정: 7/8의 NAV 하락(5,134만→3,052만)은 시세 왜곡이 아니라
7/7 SK하이닉스 전량매도(매입 2,176만원)로 포트폴리오가 실제로 줄어든 것이었다.
7/8과 7/9 이틀 연속 독립 계산이 같은 값(~3,051만)을 낸 것이 증거. 총평가
원값을 직전과 비교하던 가드·드로다운이 정당한 구성 변경을 오염으로 오판했다.
→ 비교 기준을 매입금 대비 평가배율(value/cost)로 변경: 매매·입출금은 원금과
평가가 함께 움직여 배율이 안정적이고, 시세 오염은 배율만 무너뜨린다.
"""
import os

from services.nav_service import _nav_data_suspicious, _ratio_drawdown_pct

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
    reason = _nav_data_suspicious(pnl, prev_total=50_000_000, prev_cost=15_000_000)
    assert "시세 누락" in reason and "고장난종목" in reason


def test_guard_detects_ratio_crash():
    """원금 변화 없이 평가만 무너짐 = 시세 오염 → 저장 거부."""
    pnl = [{"name": "A", "invested": 45_681_558, "current_val": 30_500_000}]
    reason = _nav_data_suspicious(
        pnl, prev_total=51_343_500, prev_cost=45_681_558
    )
    assert "평가배율 급변" in reason


def test_guard_passes_composition_change():
    """2026-07-09 오탐 실수치: 7/7 SK하이닉스 전량매도로 원금 4,568만→2,751만,
    총평가 5,134만→3,051만 (-41%). 배율은 1.124→1.109로 안정 — 정상 기록돼야 한다."""
    pnl = [{"name": "잔여3종목", "invested": 27_510_558, "current_val": 30_511_500}]
    assert _nav_data_suspicious(
        pnl, prev_total=51_343_500, prev_cost=45_681_558
    ) == ""


def test_guard_falls_back_to_raw_total_without_prev_cost():
    """직전 기록에 매입금이 없으면 원값 비교로 폴백 (보수적 유지)."""
    pnl = [{"name": "A", "invested": 27_500_000, "current_val": 30_523_000}]
    reason = _nav_data_suspicious(pnl, prev_total=51_343_500, prev_cost=None)
    assert "총평가 급변" in reason


def test_guard_passes_normal_day():
    pnl = [{"name": "A", "invested": 45_681_558, "current_val": 50_500_000}]
    assert _nav_data_suspicious(
        pnl, prev_total=51_343_500, prev_cost=45_681_558
    ) == ""


def test_guard_passes_first_record():
    pnl = [{"name": "A", "invested": 27_500_000, "current_val": 30_000_000}]
    assert _nav_data_suspicious(pnl, prev_total=None) == ""


def test_drawdown_ignores_composition_change():
    """2026-07-08 오판 실수치: 하이닉스 매도로 총평가가 5,488만→3,052만으로
    줄었지만 원금도 같이 줄었다. 원값 기준 -44.4%였던 낙폭이 배율 기준으로는
    한 자릿수여야 한다."""
    rows = [
        ("2026-07-02", 52_493_000, 45_080_554),
        ("2026-07-03", 45_681_558, 45_681_558),
        ("2026-07-06", 54_878_000, 45_681_558),   # 배율 고점 1.201
        ("2026-07-07", 51_343_500, 45_681_558),
        ("2026-07-08", 30_523_000, 27_510_558),   # 하이닉스 매도 반영 후
    ]
    dd = _ratio_drawdown_pct(rows)
    assert dd is not None and dd < 10.0, f"구성 변경이 낙폭으로 둔갑: {dd:.1f}%"


def test_drawdown_chains_across_composition_change():
    """2026-07-13/14 경보 실수치: 7/6 고점 배율 1.201은 하이닉스 매도 전 구성.
    고점/최신 배율을 그대로 비교하면 실현되어 빠져나간 이익이 낙폭에 섞여
    7/13 -15.4%(전량 단계), 7/14 -13.5%로 과대 계산됐다. 매입금이 같은 날끼리만
    체인하면 7/13 -14.3%(절반 단계), 7/14 -12.4%가 맞다 — 7/13 급락(삼성전자
    -10.7%, 삼성전기 -18.6%)은 실제였으므로 경보 자체는 유지돼야 한다."""
    rows = [
        ("2026-07-02", 52_493_000, 45_080_554),
        ("2026-07-06", 54_878_000, 45_681_558),   # 매도 전 구성 고점 (배율 1.201)
        ("2026-07-07", 51_343_500, 45_681_558),
        ("2026-07-08", 30_523_000, 27_510_558),   # 하이닉스 매도+삼성전기 반영
        ("2026-07-09", 30_511_500, 27_510_558),
        ("2026-07-10", 31_390_500, 27_510_558),
        ("2026-07-13", 27_957_500, 27_510_558),   # 실제 급락일
    ]
    dd_yesterday = _ratio_drawdown_pct(rows)
    assert dd_yesterday is not None
    assert 10.0 <= dd_yesterday < 15.0, (
        f"7/13 낙폭은 절반 단계(-14.3%)여야 함: {dd_yesterday:.1f}%"
    )
    rows.append(("2026-07-14", 28_575_500, 27_510_558))
    dd_today = _ratio_drawdown_pct(rows)
    assert dd_today is not None
    assert 10.0 <= dd_today < 13.0, (
        f"7/14 낙폭은 -12.4% 부근이어야 함: {dd_today:.1f}%"
    )


def test_drawdown_still_catches_real_crash():
    """원금 고정 상태에서 평가만 -20% → 실제 낙폭으로 잡혀야 한다."""
    rows = [
        ("2026-07-01", 50_000_000, 40_000_000),
        ("2026-07-02", 40_000_000, 40_000_000),
    ]
    dd = _ratio_drawdown_pct(rows)
    assert dd is not None and dd >= 15.0


def test_drawdown_insufficient_data():
    assert _ratio_drawdown_pct([("2026-07-01", 50_000_000, 40_000_000)]) is None
    assert _ratio_drawdown_pct([]) is None


def _history_row(date, value, cost, pnl_pct, kospi):
    return {"date": date, "total_value": value, "total_cost": cost,
            "total_pnl_pct": pnl_pct, "kospi_close": kospi,
            "kospi_pct_ytd": 0, "nav_pct_ytd": 0, "alpha_ytd": 0}


def test_weekly_report_composition_and_zero_kospi(monkeypatch):
    """2026-07-10 실수치: 기간 중 하이닉스 매도(pnl% 20.13→10.91)와 KOSPI
    수집 실패일(close=0)이 겹친 주. pnl% 차이 방식은 -9.22%, 마지막 종가 0을
    그대로 쓰면 KOSPI -100%가 나왔다. 배율 기준 -7.68%와 유효 종가 비교
    (8051.33→7246.79 = -9.99%)가 나와야 한다."""
    import services.nav_service as nav
    history = [
        _history_row("2026-07-06", 54_878_000, 45_681_558, 20.13, 8051.33),
        _history_row("2026-07-07", 51_343_500, 45_681_558, 12.39, 7656.31),
        _history_row("2026-07-08", 30_523_000, 27_510_558, 10.95, 7246.79),
        _history_row("2026-07-09", 30_511_500, 27_510_558, 10.91, 0.0),
    ]
    monkeypatch.setattr(nav, "get_nav_history", lambda days=7: history)
    report = nav.generate_nav_report(7)
    assert "-7.68%" in report, report          # 배율 기준 기간 수익률
    assert "-9.99%" in report, report          # 유효 종가끼리 KOSPI 비교
    assert "-100" not in report, report        # 종가 0 오염 차단
    assert "-9.22" not in report, report       # pnl% 차이 방식 잔재 차단


def test_weekly_report_omits_kospi_without_valid_closes(monkeypatch):
    import services.nav_service as nav
    history = [
        _history_row("2026-07-08", 30_523_000, 27_510_558, 10.95, 0.0),
        _history_row("2026-07-09", 30_511_500, 27_510_558, 10.91, 0.0),
    ]
    monkeypatch.setattr(nav, "get_nav_history", lambda days=7: history)
    report = nav.generate_nav_report(7)
    assert "비교 생략" in report
    assert "-100" not in report
