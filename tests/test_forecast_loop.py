"""구조화 예측 일지·즉시 반성·교훈 효과 검증 회귀 테스트."""
from services import forecast_loop as FL


def _fc(direction="하락", prob=65, lo=-3.0, hi=-0.5):
    return {"direction": direction, "prob": prob, "low_pct": lo, "high_pct": hi}


def test_parse_forecast_valid_and_invalid():
    ok = FL.parse_forecast('```{"방향":"하락","확률":65,"하단":-3,"상단":-0.5,"근거":"a","틀리는조건":"b"}```')
    assert ok["direction"] == "하락" and ok["low"] == -3 and ok["wrong_if"] == "b"
    assert FL.parse_forecast("죄송") is None
    assert FL.parse_forecast('{"방향":"상승","확률":30,"하단":0,"상단":1}') is None          # 확률 범위 밖
    assert FL.parse_forecast('{"방향":"상승","확률":60,"하단":-3,"상단":-1}') is None         # 방향-범위 모순
    assert FL.parse_forecast('{"방향":"하락","확률":60,"하단":1,"상단":3}') is None
    assert FL.parse_forecast('{"방향":"하락","확률":60,"하단":-20,"상단":3}') is None         # 비현실적 폭


def test_score_one_diagnoses_amplitude_underestimate():
    r = FL.score_one(_fc(), -6.02)
    assert r["dir_hit"] == 1 and r["range_hit"] == 0
    assert "변동폭 과소평가" in r["reflection"] and "6.0%" in r["reflection"]
    assert r["brier"] == round((0.65 - 1) ** 2, 4)


def test_score_one_flags_overconfidence_on_miss():
    r = FL.score_one(_fc(prob=80), 1.5)
    assert r["dir_hit"] == 0 and "과신" in r["reflection"] and r["brier"] == round(0.64, 4)


def test_scorecard_baselines_and_edge():
    rows = []
    for i, (act, prev) in enumerate([(-2.0, -1.0), (-1.0, -2.0), (1.5, -1.0), (-1.2, 1.0)]):
        sc1 = FL.score_one(_fc(), act)
        rows.append({**sc1, "prev_chg": prev, "date": f"d{i}", "prob": 65,
                     "low_pct": -3.0, "high_pct": -0.5})
    sc = FL.scorecard(rows)
    assert sc["n"] == 4 and sc["dir_pct"] == 75
    assert sc["major_pct"] == 75                       # 하락이 3/4
    assert sc["persist_pct"] == 50                     # 전일 방향 반복 기준선
    assert sc["edge"] == 0 and "표본이 적어" in FL.format_scorecard_line(sc)


def test_lesson_verdict_never_decides_on_thin_data():
    assert FL.lesson_verdict(0, None, 10, 70.0) == "pending"        # 비교 대상 없음
    assert FL.lesson_verdict(10, 50.0, 5, 90.0) == "pending"        # 생성 후 표본 부족
    assert FL.lesson_verdict(10, 50.0, 10, 60.0) == "confirmed"
    assert FL.lesson_verdict(10, 60.0, 10, 45.0) == "retired"
    assert FL.lesson_verdict(10, 55.0, 10, 52.0) == "neutral"


def test_record_score_and_evaluate_roundtrip(monkeypatch):
    from db.database import get_conn
    from sqlalchemy import text
    fc = {"direction": "하락", "prob": 70.0, "low": -3.0, "high": -0.5, "reason": "r", "wrong_if": "w", "model": "independent"}
    with get_conn() as c:    # 테스트 DB 파일이 실행 간 유지되므로 자기 행을 먼저 정리 (멱등)
        c.execute(text("DELETE FROM forecast_log WHERE date='2026-01-05'"))
    assert FL.record_forecast("2026-01-05", "pre_market", fc, -1.0, [1])
    assert FL.record_forecast("2026-01-05", "pre_market", {**fc, "prob": 60.0}, -1.0, [1])   # 같은 날 재실행 = 갱신
    with get_conn() as c:
        assert c.execute(text("SELECT COUNT(*) FROM forecast_log WHERE date='2026-01-05'")).scalar() == 1
    closes = {"2026-01-02": 100.0, "2026-01-05": 98.0}
    assert FL.score_forecasts(today="2026-01-06", fetcher=lambda a, b: closes) == 1
    rows = [r for r in FL.load_scored() if r["date"] == "2026-01-05"]
    assert rows and rows[0]["dir_hit"] == 1 and "방향 적중" in rows[0]["reflection"]


def test_cycle_section_has_all_parts():
    rows = [{**FL.score_one(_fc(), -6.0), "date": "2026-10-14", "prev_chg": -3.0, "prob": 65,
             "low_pct": -3.0, "high_pct": -0.5, "wrong_if": "S&P 반등"}]
    ls = [{"lesson": "급변 직후 중립 금지", "status": "pending", "hit_before": None, "hit_after": None,
           "n_before": 0, "n_after": 1}]
    txt = FL.format_cycle_section(rows, FL.scorecard(rows), ls)
    for key in ("예측 → 결과 → 반성", "독립 예측가 성적", "가장 크게 빗나간 예측", "교훈의 효과", "검증 대기"):
        assert key in txt
    assert FL.format_cycle_section([], {}, []) == ""


def test_forecast_for_briefing_never_raises(monkeypatch):
    monkeypatch.setattr(FL, "make_forecast", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert FL.forecast_for_briefing("2026-01-06", "pre_market", {}, "") == ""
    monkeypatch.setattr(FL, "make_forecast", lambda *a, **k: None)
    assert FL.forecast_for_briefing("2026-01-06", "pre_market", {}, "") == ""
