"""학습 고리 회귀 테스트 — 소급 채점·성적표·교훈 파싱·표본 게이트."""
from services import learning_loop as L


def _row(d, pdir, adir, prob=50.0, chg=0.0):
    return {"date": d, "predicted_dir": pdir, "predicted_prob": prob,
            "actual_dir": adir, "actual_kospi": chg, "correct": 1 if pdir == adir else 0}


def test_daily_changes_uses_previous_close_and_skips_first():
    ch = L.daily_changes({"2026-10-01": 100.0, "2026-10-02": 101.0, "2026-10-06": 99.99})
    assert "2026-10-01" not in ch
    assert ch["2026-10-02"] == 1.0
    assert ch["2026-10-06"] == round((99.99 - 101) / 101 * 100, 2)


def test_direction_band():
    assert L._to_direction(0.3) == "상승"
    assert L._to_direction(-0.3) == "하락"
    assert L._to_direction(0.29) == "중립"


def test_scorecard_baseline_and_neutral_insight():
    rows = [_row(f"d{i}", "중립", "하락", chg=-1.5) for i in range(5)]
    rows += [_row(f"u{i}", "상승", "상승", chg=1.2) for i in range(4)]
    sc = L.market_scorecard(rows)
    assert sc["n"] == 9 and sc["hits"] == 4
    assert sc["baseline_dir"] == "하락" and sc["baseline_pct"] == 56
    txt = L.format_scorecard(sc)
    assert "중립' 예측은 5건 중 적중 0%" in txt
    assert "표본 9건은 적다" in txt


def test_scorecard_empty_below_threshold():
    sc = L.market_scorecard([_row("a", "상승", "상승")] * 3)
    assert L.format_scorecard(sc) == ""          # 표본 부족 → 프롬프트 무동작
    assert L.reflect([], sc, chat_fn=lambda *a, **k: "x") == []


def test_parse_reflection_handles_garbage_and_valid():
    assert L.parse_reflection("죄송합니다") == []
    assert L.parse_reflection("[깨진 json") == []
    raw = '```json\n[{"분석":"a","사고":"b","판단":"c","교훈":"중립 줄이기","의견":"d"}]\n```'
    out = L.parse_reflection(raw)
    assert len(out) == 1 and out[0]["lesson"] == "중립 줄이기" and out[0]["opinion"] == "d"


def test_reflect_drops_lessons_with_invented_numbers():
    rows = [_row(f"d{i}", "중립", "하락", chg=-1.5) for i in range(9)]
    sc = L.market_scorecard(rows)
    raw = ('[{"분석":"중립 9건 적중 0%","사고":"x","판단":"y","교훈":"중립을 줄인다","의견":"z"},'
           '{"분석":"a","사고":"x","판단":"y","교훈":"승률 87%와 알파 12.3%p 33% 41% 52% 63%를 노린다","의견":"z"}]')
    out = L.reflect(rows, sc, chat_fn=lambda *a, **k: raw)
    assert [x["lesson"] for x in out] == ["중립을 줄인다"]


def test_lessons_roundtrip_and_ttl():
    L.save_lessons([{"kind": "market", "lesson": "TEST-교훈-A", "evidence": "e"}], "2026-01-01")
    assert L.save_lessons([{"kind": "market", "lesson": "TEST-교훈-A"}], "2026-01-01") == 0   # 중복 방지
    assert any(x["lesson"] == "TEST-교훈-A" for x in L.active_lessons(today="2026-01-10"))
    assert not any(x["lesson"] == "TEST-교훈-A" for x in L.active_lessons(today="2026-06-01"))  # 60일 경과


def test_weekly_message_has_all_stages():
    sc = {"n": 50, "hit_pct": 52, "baseline_pct": 52}
    msg = L.format_weekly_message(sc, [{"lesson": "L", "analysis": "A", "thought": "T", "opinion": "O"}])
    for key in ("분석:", "사고:", "판단→교훈:", "의견:"):
        assert key in msg


def test_investigation_facts_include_conditions_and_actual():
    pred = {"date": "2026-10-12", "predicted_dir": "중립", "predicted_prob": 50.0,
            "actual_dir": "하락", "actual_kospi": -3.65, "tail_risk": "외국인 매도"}
    before = {"kospi_chg": -2.62, "usd_krw": 1341.3, "vix": 14.84, "us10y": 5.244, "sp500_chg": 0.13, "nasdaq_chg": 0.27}
    txt = L.build_investigation_facts(pred, before, None, ["요약A"])
    assert "-3.65%" in txt and "-2.62%" in txt and "다음 영업일 장전 조건] 데이터 없음" in txt and "요약A" in txt


def test_miss_cases_show_investigation_result():
    r = {"date": "d", "predicted_dir": "중립", "predicted_prob": 50.0, "actual_dir": "하락",
         "actual_kospi": -2.0, "correct": 0, "miss_reason": "[AI 조사] 가설: 신호 간과"}
    assert "원인 조사: [AI 조사] 가설: 신호 간과" in L._miss_cases([r])
