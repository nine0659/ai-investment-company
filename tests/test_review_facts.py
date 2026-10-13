from services.review_facts import build_review_facts
import agents.review_feedback_team as rft


def _r(date, name, ret):
    return {"date": date, "code": "X", "name": name, "return_pct": ret}


def test_empty_forbids_patterns():
    assert "패턴·승률을 언급하지 말 것" in build_review_facts([])


def test_small_sample_warns_and_weekday_stats():
    recs = [_r("2026-08-30", "A", 3.0), _r("2026-08-30", "B", -1.0), _r("2026-08-30", "C", 2.0)]
    out = build_review_facts(recs)
    assert "표본 경고" in out
    assert "일 3건 평균 +1.3%" in out  # 2026-08-30은 일요일
    assert "서로 다른 종목 3개" in out


def test_concentration_detected():
    recs = [_r("2026-09-01", "A", 1.0)] * 5 + [_r("2026-09-02", "B", 1.0)] * 4
    out = build_review_facts(recs)
    assert "최다 추천 A 5건" in out
    assert "표본: 9건" in out


def test_review_prompt_no_longer_asks_llm_for_scores():
    assert "X/10점" not in rft._SYSTEM
    assert "새 통계를 만들거나 계산하지 마세요" in rft._SYSTEM
