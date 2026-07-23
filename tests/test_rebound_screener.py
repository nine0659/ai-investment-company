"""기술적·추세적 반등 스크리너 회귀 테스트.

LLM을 쓰지 않는 순수 계산 기능이라 전부 결정론적으로 테스트 가능 — 이게 이
기능을 만든 이유이기도 하다(오판 여지 최소화).
"""
from agents.rebound_screener_agent import classify_signal, screen, _format_report


def _chart(**overrides):
    base = {
        "ma_aligned": False, "golden_cross": False,
        "vol_ratio": 1.0, "pos_52w": 50.0,
        "bb_break_up": False, "bb_break_down": False,
        "chart_score": 0,
    }
    base.update(overrides)
    return base


def test_trend_rebound_requires_full_alignment_low_position_and_volume():
    ch = _chart(ma_aligned=True, vol_ratio=1.8, pos_52w=40.0)
    assert classify_signal(ch) == "추세적 반등"


def test_technical_rebound_is_golden_cross_without_full_alignment():
    ch = _chart(golden_cross=True, ma_aligned=False, vol_ratio=2.0, pos_52w=30.0)
    assert classify_signal(ch) == "기술적 반등"


def test_still_falling_excluded_even_with_golden_cross():
    """볼린저밴드 하단 이탈 중(아직 낙폭 진행)이면 반등으로 안 본다."""
    ch = _chart(golden_cross=True, vol_ratio=2.0, pos_52w=20.0, bb_break_down=True)
    assert classify_signal(ch) is None


def test_low_volume_not_confirmed_excluded():
    ch = _chart(golden_cross=True, vol_ratio=1.1, pos_52w=30.0)
    assert classify_signal(ch) is None


def test_already_near_52w_high_excluded():
    """이미 52주 고점 근처면 '반등'이 아니라 '이미 오른 상태' — 추격 매수 방지."""
    ch = _chart(ma_aligned=True, vol_ratio=2.0, pos_52w=95.0)
    assert classify_signal(ch) is None


def test_no_signal_flat_chart():
    assert classify_signal(_chart()) is None


def test_screen_sorts_by_chart_score_and_caps_top_n(monkeypatch):
    import agents.rebound_screener_agent as mod

    fake_charts = {
        "000001": _chart(ma_aligned=True, vol_ratio=2.0, pos_52w=30.0, chart_score=80),
        "000002": _chart(ma_aligned=True, vol_ratio=1.6, pos_52w=40.0, chart_score=50),
        "000003": _chart(golden_cross=True, vol_ratio=2.0, pos_52w=20.0, chart_score=60),
    }
    monkeypatch.setattr(mod, "analyze_chart", lambda code, name: fake_charts[code])

    pool = [
        {"code": "000001", "name": "A", "market": "KOSPI"},
        {"code": "000002", "name": "B", "market": "KOSPI"},
        {"code": "000003", "name": "C", "market": "KOSDAQ"},
    ]
    result = screen(pool)
    trend = result["추세적 반등"]
    assert [c["code"] for c in trend] == ["000001", "000002"]
    assert result["기술적 반등"][0]["code"] == "000003"


def test_format_report_shows_no_signal_message_when_empty():
    report = _format_report({"추세적 반등": [], "기술적 반등": []})
    assert "해당 없음" in report
    assert "LLM 서술 없음" in report


def test_empty_raw_pool_reported_as_data_failure_not_no_candidates(monkeypatch):
    """2026-07-23 실사고 재현: KIS 하락률 순위가 원본부터 비면 '해당없음'이 아니라
    데이터 수집 실패로 알려야 한다 — 실제로 후보가 없는 상황은 극히 드물다."""
    import agents.rebound_screener_agent as mod

    monkeypatch.setattr(mod, "_decliner_pool", lambda kis: [])

    sent = []
    monkeypatch.setattr("clients.telegram_client.send_message", lambda text: sent.append(text))

    report = mod.run_rebound_screen(send=True)

    assert "데이터 수집" in report
    assert "해당 없음" not in report
    assert sent and "데이터 수집" in sent[0]
