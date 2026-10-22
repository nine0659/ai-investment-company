"""업종 온도계 회귀 테스트 — 시장 대비 분류(지수 수준과 무관), 최소 표본, 저장/비교, 일일 요약·컨텍스트 배선."""
import pytest
from sqlalchemy import text

from db.database import get_conn, init_db
from services import industry_board as ib


def _row(code, ind, close=100.0, ret_1m=0.0, ret_3m=0.0, ma50=100.0, ma200=100.0, eps=100.0, cns_eps=110.0, cns_per=8.0, fb=2):
    return {"code": code, "close": close, "detail": {
        "industry": ind, "eps": eps, "cns_eps": cns_eps, "cns_per": cns_per, "flow_days": 3, "foreign_buy_days": fb,
        "hist": {"ret_1m": ret_1m, "ret_3m": ret_3m, "ma50": ma50, "ma200": ma200}}}


def _ind(prefix, ind, n, **kw):
    return [_row(f"{prefix}{i}", ind, **kw) for i in range(n)]


_GROUPS = [{"no": 1, "name": "반도체", "changeRate": "1.5"}, {"no": 2, "name": "건설", "changeRate": "-0.7"},
           {"no": 3, "name": "화학", "changeRate": "0.2"}, {"no": 4, "name": "소규모", "changeRate": "0"}]


def test_regimes_are_relative_to_market_not_absolute():
    """시장 전체가 -40%여도 시장 대비 강한 업종은 '강세'로 잡혀야 한다(2026-10 시장 상태가 그랬다)."""
    rows = (_ind("a", "1", 8, ret_3m=-5.0, ret_1m=3.0, close=120, ma200=100)               # 시장 대비 강함 + 200일선 위
            + _ind("b", "2", 8, ret_3m=-45.0, ret_1m=-5.0, close=60, ma50=100, ma200=100)   # 시장 대비 약함
            + _ind("c", "3", 8, ret_3m=-25.0, ret_1m=1.0, close=105, ma50=100, ma200=130))  # 중간 + 1개월 반등
    board = ib.build(rows, _GROUPS)
    reg = {i["name"]: i["regime"] for i in board["industries"]}
    assert reg["반도체"] == "강세" and reg["건설"] == "약세" and reg["화학"] == "회복초입"


def test_small_industries_excluded():
    board = ib.build(_ind("a", "1", 8) + _ind("z", "4", 3), _GROUPS)
    assert [i["name"] for i in board["industries"]] == ["반도체"]


def test_metrics_computed_by_code():
    rows = _ind("a", "1", 4, close=120, ma50=100, ma200=100, ret_1m=2.0, ret_3m=10.0, cns_eps=150, fb=3) \
        + _ind("b", "1", 2, close=80, ma50=100, ma200=100, ret_1m=-2.0, ret_3m=-10.0, cns_eps=100, fb=0)
    i = ib.build(rows, _GROUPS)["industries"][0]
    assert i["n"] == 6 and i["pct_above_ma200"] == pytest.approx(4 / 6 * 100)
    assert i["med_ret_3m"] == pytest.approx(10.0) and i["med_growth"] == pytest.approx(50.0)
    assert i["foreign_buy_pct"] == pytest.approx(4 / 6 * 100) and i["change_rate"] == 1.5


def test_missing_history_gives_neutral_not_crash():
    rows = _ind("a", "1", 6)
    for r in rows:
        r["detail"]["hist"] = None
    board = ib.build(rows, _GROUPS)
    assert board["industries"][0]["regime"] == "중립" and board["industries"][0]["med_ret_3m"] is None


def test_unknown_group_name_falls_back():
    assert ib.build(_ind("a", "999", 6), _GROUPS)["industries"][0]["name"] == "업종999"


@pytest.fixture
def db():
    init_db()
    with get_conn() as c:
        c.execute(text("DELETE FROM industry_daily"))
    yield
    with get_conn() as c:
        c.execute(text("DELETE FROM industry_daily"))


def _board_with(regime_rows):
    return {"market": {}, "industries": regime_rows}


def _item(no, name, regime, r3=10.0, r1=2.0, a50=60.0, a200=60.0):
    return {"ind_no": no, "name": name, "n": 10, "change_rate": 1.0, "med_ret_1m": r1, "med_ret_3m": r3,
            "pct_above_ma50": a50, "pct_above_ma200": a200, "med_cns_per": 8.0, "med_growth": 20.0,
            "foreign_buy_pct": 50.0, "regime": regime}


def test_save_load_replace_same_day(db):
    assert ib.save_daily(_board_with([_item("1", "반도체", "강세")]), date="2026-10-22") == 1
    ib.save_daily(_board_with([_item("1", "반도체", "강세"), _item("2", "건설", "약세")]), date="2026-10-22")
    snaps = ib.load_snapshots()
    assert len(snaps) == 1 and len(snaps[0][1]) == 2


def test_format_shows_sections_and_change_vs_oldest(db):
    ib.save_daily(_board_with([_item("1", "반도체", "강세", a50=40.0), _item("2", "건설", "약세", r3=-40.0)]), date="2026-10-20")
    ib.save_daily(_board_with([_item("1", "반도체", "강세", a50=70.0), _item("2", "건설", "약세", r3=-40.0),
                               _item("3", "화학", "회복초입", r3=-20.0, r1=4.0)]), date="2026-10-22")
    txt = ib.format_board()
    assert "🔥 강세 업종" in txt and "🌱 회복 초입 업종" in txt and "🧊 약세 업종" in txt
    assert "2026-10-20 대비 변화" in txt and "40%→70%(개선)" in txt


def test_format_empty_when_no_snapshot(db):
    assert ib.format_board() == ""


def test_format_empty_when_all_neutral(db):
    ib.save_daily(_board_with([_item("1", "반도체", "중립")]), date="2026-10-22")
    assert ib.format_board() == ""


def test_digest_includes_board_only_when_today(db, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from services import daily_digest as dd
    today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")
    ib.save_daily(_board_with([_item("1", "반도체", "강세")]), date=today)
    with get_conn() as c:
        c.execute(text("DELETE FROM universe_screen_daily"))
        c.execute(text("DELETE FROM us_screen_daily"))
        c.execute(text("INSERT INTO universe_screen_daily (date, code, name, market, close, mcap_eok, score, signals) "
                       "VALUES (:d, '111111', 'A', 'KOSPI', 100, 5000, 4, 'x')"), {"d": today})
    r = dd.build()
    assert "업종 온도계" in r["text"] and r["counts"]["board"] == 1
    with get_conn() as c:
        c.execute(text("DELETE FROM universe_screen_daily"))
        c.execute(text("DELETE FROM industry_daily"))
    ib.save_daily(_board_with([_item("1", "반도체", "강세")]), date="2020-01-01")
    assert "업종 온도계" not in (dd.build()["text"] or "")


def test_naver_industry_groups_parsing(monkeypatch):
    from clients import naver_client as nv
    monkeypatch.setattr(nv, "_get", lambda url: {"groups": [{"no": 1, "name": "x"}, {"no": None}, "bad"]})
    assert nv.industry_groups() == [{"no": 1, "name": "x"}]
    monkeypatch.setattr(nv, "_get", lambda url: None)
    assert nv.industry_groups() == []


def test_naver_industry_groups_request_uses_valid_page_size(monkeypatch):
    """pageSize=200은 네이버가 400으로 거부한다(상한 100) — 이름 조회가 조용히 실패해 '업종282'로 표시되던 사고의 회귀 방지."""
    from clients import naver_client as nv
    urls = []
    monkeypatch.setattr(nv, "_get", lambda url: urls.append(url) or {"groups": []})
    nv.industry_groups()
    import re
    assert int(re.search(r"pageSize=(\d+)", urls[0]).group(1)) <= 100


def test_names_flag_recorded_when_groups_missing(monkeypatch):
    from services import universe_screener as us
    from clients import naver_client as nv
    monkeypatch.setattr(nv, "industry_groups", lambda: [])
    board = us._industry_board(_ind("a", "1", 6))
    assert board["names_ok"] is False and board["industries"][0]["name"] == "업종1"
