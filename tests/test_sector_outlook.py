"""업종 전망 엔진 회귀 테스트 — 근거 계산, 창작 숫자 폐기, 적중 판정, 채점 기준선, 컨텍스트."""
import json

import pytest
from sqlalchemy import text

from db.database import get_conn, init_db
from services import sector_outlook as so


@pytest.fixture(autouse=True)
def _db():
    init_db()
    with get_conn() as c:
        for t in ("sector_outlook", "universe_screen_daily", "industry_daily"):
            c.execute(text(f"DELETE FROM {t}"))


def _ind(no, name, n=8, per=8.0, growth=10.0, fb=60.0, b200=50.0, regime="중립"):
    return {"date": "d", "ind_no": no, "name": name, "n": n, "change_rate": 0, "med_ret_1m": 0, "med_ret_3m": 0,
            "pct_above_ma50": 50, "pct_above_ma200": b200, "med_cns_per": per, "med_growth": growth,
            "foreign_buy_pct": fb, "regime": regime}


MKT = {**_ind("ALL", "전체 시장", n=1000, per=10.0, growth=5.0, fb=40.0, b200=40.0), "regime": "-"}


def _snaps():
    cur = [MKT, _ind("1", "반도체", per=7.0, growth=20.0, fb=70.0, b200=60.0), _ind("2", "건설", per=12.0, growth=-5.0),
           _ind("3", "작은업종", n=3)]
    old = [MKT, _ind("1", "반도체", b200=50.0), _ind("2", "건설")]
    return [("2026-10-22", cur), ("2026-10-15", old)]


def test_evidence_computed_relative_to_market_and_small_excluded():
    date, rows, ev = so.evidence_rows(_snaps())
    r = {x["name"]: x for x in rows}
    assert date == "2026-10-22" and "작은업종" not in r
    assert r["반도체"]["per_disc"] == pytest.approx(-30.0)       # 7/10-1
    assert r["반도체"]["growth_rel"] == pytest.approx(15.0)
    assert r["반도체"]["b200_chg"] == pytest.approx(10.0)
    assert "1 반도체" in ev


def test_no_market_row_gives_nothing():
    assert so.evidence_rows([("d", [_ind("1", "x")])]) == ("", [], "")


def _raw(*items):
    return json.dumps({"판단": list(items)}, ensure_ascii=False)


def _it(no="1", view="우호", prob=60, reason="PER 할인 -30.0%", wi="이익기대 10.0 아래로"):
    return {"업종번호": no, "전망": view, "확률": prob, "근거": reason, "틀리는조건": wi}


def test_parse_rejects_invented_numbers_unknown_and_bad_probs():
    _, rows, ev = so.evidence_rows(_snaps())
    ok = so.parse_views(_raw(_it()), rows, ev)
    assert len(ok) == 1 and ok[0]["name"] == "반도체"
    assert so.parse_views(_raw(_it(reason="영업이익 77.7% 급증")), rows, ev) == []     # 근거에 없는 숫자
    assert len(so.parse_views(_raw(_it(wi="PER이 50% 이상 오르면")), rows, ev)) == 1   # 틀리는조건의 기준값은 새 숫자여도 정상(실모델 시험에서 전부 폐기돼 0건이었음)
    assert so.parse_views(_raw(_it(no="999")), rows, ev) == []                          # 모르는 업종
    assert so.parse_views(_raw(_it(prob=95)), rows, ev) == []                           # 과신
    assert so.parse_views(_raw(_it(view="중립")), rows, ev) == []
    assert len(so.parse_views(_raw(_it(), _it()), rows, ev)) == 1                       # 중복 업종
    assert so.parse_views("not json", rows, ev) == []


def test_hit_requires_clearing_band():
    assert so.score_hit("우호", 2.0) == 1 and so.score_hit("우호", 0.5) == 0 and so.score_hit("우호", -3) == 0
    assert so.score_hit("비우호", -2.0) == 1 and so.score_hit("비우호", -0.5) == 0


def _seed_universe(dates, industries_up=True):
    """종목 120개: 업종 '1' 6개는 날짜마다 +1%씩, 나머지는 제자리."""
    with get_conn() as c:
        for k, d in enumerate(dates):
            for i in range(120):
                ind = "1" if i < 6 else "9"
                close = 100 * (1.01 ** k) if ind == "1" else 100.0
                c.execute(text("INSERT INTO universe_screen_daily (date, code, close, industry) VALUES (:d,:c,:p,:i)"),
                          {"d": d, "c": f"{i:06d}", "p": close, "i": ind})


def test_score_pending_uses_member_median_minus_all_median(monkeypatch):
    dates = [f"2026-09-{d:02d}" for d in range(1, 29)]       # 28개
    _seed_universe(dates)
    so.record_views(dates[0], [{"ind_no": "1", "name": "반도체", "view": "우호", "prob": 60, "reason": "r", "wrong_if": "w"}], "ev")
    so.record_views(dates[1], [{"ind_no": "1", "name": "반도체", "view": "비우호", "prob": 60, "reason": "r", "wrong_if": "w"}], "ev")
    assert so.score_pending() == 2
    with get_conn() as c:
        rows = c.execute(text("SELECT view, actual_rel, hit FROM sector_outlook ORDER BY date")).fetchall()
    assert rows[0][1] == pytest.approx((1.01 ** 20 - 1) * 100) and rows[0][2] == 1    # 우호 적중
    assert rows[1][2] == 0                                                              # 비우호 미적중


def test_not_scored_before_horizon():
    dates = [f"2026-09-{d:02d}" for d in range(1, 11)]
    _seed_universe(dates)
    so.record_views(dates[0], [{"ind_no": "1", "name": "x", "view": "우호", "prob": 60, "reason": "r", "wrong_if": "w"}], "ev")
    assert so.score_pending() == 0


def test_scorecard_flags_small_sample():
    so.record_views("2026-09-01", [{"ind_no": "1", "name": "x", "view": "우호", "prob": 60, "reason": "r", "wrong_if": "w"}], "ev")
    with get_conn() as c:
        c.execute(text("UPDATE sector_outlook SET hit=1, actual_rel=3"))
    sc = so.scorecard()
    assert sc["n"] == 1 and not sc["trusted"]
    assert "표본" in so.format_scorecard_line(sc)
    assert "아직 없음" in so.format_scorecard_line({"n": 0})


def test_run_daily_records_and_context(monkeypatch):
    with get_conn() as c:
        c.execute(text("INSERT INTO universe_screen_daily (date, code, close, industry) VALUES ('2026-10-22','000001',100,'1')"))
    msg = so.run_daily(snaps=_snaps(), chat_fn=lambda s, u, max_tokens=0: _raw(_it()))
    assert "1건 기록" in msg
    monkeypatch.setattr(so, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: __import__("datetime").datetime(2026, 10, 23)),
                                                       "strptime": staticmethod(__import__("datetime").datetime.strptime)}))
    blk = so.context_block()
    assert "반도체: 우호 60%" in blk and "틀리는 조건" in blk and "확신도를 높이는 근거로 쓰지 말고" in blk


def test_run_daily_skips_when_snapshot_has_no_industry():
    msg = so.run_daily(snaps=_snaps(), chat_fn=lambda *a, **k: pytest.fail("LLM 호출되면 안 됨"))
    assert "건너뜀" in msg


def test_truncated_json_keeps_complete_items():
    """2026-10-23 실모델 시험: 출력이 길어 JSON이 중간에 잘리면 전체가 폐기돼 유효 판단 0건이었다 — 완결된 객체는 살린다."""
    _, rows, ev = so.evidence_rows(_snaps())
    full = _raw(_it(), _it(no="2", view="비우호", reason="PER 할인 +20.0%"))
    cut = full[: full.rfind("{") + 25]                       # 두 번째 객체 중간에서 잘림
    got = so.parse_views(cut, rows, ev)
    assert len(got) == 1 and got[0]["name"] == "반도체"
    fenced = "```json\n" + full + "\n```"
    assert len(so.parse_views(fenced, rows, ev)) == 2
