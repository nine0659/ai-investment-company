"""시장 전체 DART 공시 사건 — 분류 규칙·본문 파싱·극성·스크리너 연동 회귀 테스트.

실측 중 잡은 함정(2026-10-22): ① '주권매매거래정지해제'·'파산신청기각'·'상장유지'가 악재로 잡힘 ② 공급계약 때문의 일시 거래정지가
악재로 잡힘 ③ 적자전환 잠정실적이 증감률이 없어 중립으로 분류됨 ④ 정정공시가 원공시와 중복.
"""
import pytest

from services import disclosure_events as de
from services import universe_screener as us


@pytest.mark.parametrize("title,expect", [
    ("주요사항보고서(유상증자결정)", ("유상증자", "악재")),
    ("[기재정정]주요사항보고서(유상증자결정)", None),
    ("주요사항보고서(전환사채권발행결정)", ("메자닌발행", "악재")),
    ("주요사항보고서(자기주식취득결정)", ("자사주취득", "호재")),
    ("주식소각결정", ("자사주소각", "호재")),
    ("현금ㆍ현물배당결정", ("배당결정", "호재")),
    ("단일판매ㆍ공급계약체결", ("공급계약", "호재")),
    ("연결재무제표기준영업(잠정)실적(공정공시)", ("잠정실적", "중립")),
    ("횡령ㆍ배임혐의발생", ("횡령·배임·회계위반", "악재")),
    ("불성실공시법인지정(공시번복)", ("불성실공시", "악재")),
    ("기타시장안내(상장적격성 실질심사 대상 결정)", ("상폐·부도위험", "악재")),
    ("주권매매거래정지(상장폐지 사유발생)", ("상폐·부도위험", "악재")),
    ("투자유의안내 (관리종목지정 우려 예고)", ("상폐·부도위험", "악재")),
    ("주권매매거래정지해제(액면병합)", ("위험해소·절차", "중립")),
    ("파산신청기각", ("위험해소·절차", "중립")),
    ("기타시장안내(상장폐지 이의신청서 제출)", ("위험해소·절차", "중립")),
    ("주권매매거래정지(단일판매공급계약)", None),
    ("기업설명회(IR)개최(안내공시)", None),
    ("주주총회소집결의", None),
    ("전환가액의조정", None),
    ("", None),
])
def test_classify(title, expect):
    assert de.classify(title) == expect


_SUPPLY = ["단일판매ㆍ공급계약 체결", "1. 판매ㆍ공급계약 구분", "기타 판매ㆍ공급계약", "- 체결계약명", "선박엔진 공급계약",
           "2. 계약내역", "계약금액(원)", "26,825,600,000", "최근매출액(원)", "402,396,221,904", "매출액대비(%)", "6.7",
           "대규모법인여부", "미해당", "3. 계약상대", "JIANGSU NEW YANGZI SHIPBUILDING COMPANY LIMITED"]


def test_parse_supply_contract():
    d = de.parse_supply_contract(_SUPPLY)
    assert d["amount"] == 26_825_600_000 and d["sales_pct"] == 6.7 and d["recent_sales"] == 402_396_221_904
    assert d["counterparty"].startswith("JIANGSU")
    s, m = de.summarize_supply("HD현대마린엔진", d)
    assert "268억원" in s and "6.7%" in s and m == 6.7


def test_parse_supply_missing_amount_is_not_invented():
    s, m = de.summarize_supply("X", de.parse_supply_contract(["계약금액(원)", "-"]))
    assert m is None and "확인 못 함" in s


def _earn(op_vals, sales_vals=("1,082,080", "914,640", "18.3", "-", "1,053,003", "2.8", "-")):
    return (["단위 : 백만원, %", "실적기간", "당기실적", "2026-07-01", "~", "2026-09-30", "매출액", "당해실적", *sales_vals,
             "누계실적", "2,670,665", "-", "-", "-", "3,121,962", "-14.5", "-",
             "영업이익", "당해실적", *op_vals, "누계실적", "368,622", "-", "-", "-", "207,265", "77.9", "-"])


def test_parse_earnings_growth():
    d = de.parse_earnings(_earn(("165,762", "122,719", "35.1", "-", "72,987", "127.1", "-")))
    op = d["rows"]["영업이익"]
    assert d["unit"] == "백만원" and op["cur"] == 165762 and op["yoy"] == 127.1 and op["qoq"] == 35.1
    s, m = de.summarize_earnings(d)
    assert "+127.1%" in s and m == 127.1
    # 영업이익이 크게 늘어도 '호재'가 아니다: 과거 검증에서 이런 발표 이후 20~60일은 오히려 시장보다 부진했다(+100%↑ 60일 t=-2.8)
    assert de.earnings_polarity(d, m) == "중립"


def test_earnings_turn_to_loss_is_bad_even_without_growth_rate():
    d = de.parse_earnings(_earn(("-2,137", "1,500", "-242.5", "적자전환", "3,000", "-", "적자전환")))
    s, m = de.summarize_earnings(d)
    assert m is None and "적자전환" in s
    assert de.earnings_polarity(d, m) == "악재"


def test_earnings_turn_to_profit_is_not_called_good_news():
    d = de.parse_earnings(_earn(("500", "-100", "-", "흑자전환", "-300", "-", "흑자전환")))
    assert de.earnings_polarity(d, None) == "중립"            # 흑자전환도 검증된 초과수익 없음(t≈+1) → 호재로 단정하지 않는다


def test_earnings_flat_is_neutral():
    d = de.parse_earnings(_earn(("640", "590", "9.1", "-", "633", "1.0", "-")))
    assert de.earnings_polarity(d, 1.0) == "중립"


def _item(nm, code="005930", no="20261022000001"):
    return {"report_nm": nm, "stock_code": code, "corp_name": "테스트", "rcept_no": no, "rcept_dt": "20261022",
            "corp_cls": "K"}


def test_build_event_supply_contract_below_5pct_is_neutral():
    toks = [t if t != "6.7" else "3.1" for t in _SUPPLY]
    ev = de.build_event(_item("단일판매ㆍ공급계약체결"), toks)
    assert ev["event_type"] == "공급계약" and ev["polarity"] == "중립" and ev["metric"] == 3.1
    assert ev["market"] == "KOSDAQ" and ev["date"] == "2026-10-22"


def test_build_event_supply_contract_large_is_positive():
    toks = [t if t != "6.7" else "19.2" for t in _SUPPLY]
    assert de.build_event(_item("단일판매ㆍ공급계약체결"), toks)["polarity"] == "호재"


def test_build_event_ignores_irrelevant_and_no_stock_code():
    assert de.build_event(_item("기업설명회(IR)개최(안내공시)")) is None
    assert de.build_event(_item("주요사항보고서(유상증자결정)", code="")) is None


def test_build_event_survives_parse_failure():
    ev = de.build_event(_item("단일판매ㆍ공급계약체결"), ["엉뚱한", "토큰"])
    assert ev["event_type"] == "공급계약" and "확인 못 함" in ev["summary"]


# ── 스크리너 연동 ────────────────────────────────────────────────────────────

def _ev(typ, pol, summary="", metric=None, date="2026-10-21"):
    return {"event_type": typ, "polarity": pol, "summary": summary, "metric": metric, "date": date}


def test_event_signals_scored_only_for_validated_events():
    """과거 검증(scripts/backtest_event_signals.py dart): 자사주 취득만 통과(비금융 20일 +1.74%p, t=+3.4) → 점수 신호.
    대형 공급계약·잠정실적 호조는 미검증 → '(참고·미검증)' info 신호로만 표시하고 점수에서 제외."""
    sig = us.event_signals([_ev("공급계약", "호재", "계약금액 500억원 · 최근매출액 대비 19.2%", 19.2),
                            _ev("자사주취득", "호재"), _ev("공급계약", "호재", "계약금액 100억원", 4.0),
                            _ev("잠정실적", "호재", "영업이익 +127.1%"), _ev("잠정실적", "중립", "x")])
    assert not any("잠정실적" in s["text"] for s in sig)                       # 실적 발표는 호재 신호로 쓰지 않는다
    assert len(sig) == 2 and all(s["key"] == "공시호재" for s in sig)          # 최대 2개
    assert not sig[0].get("info") and "자사주 취득" in sig[0]["text"]           # 검증된 것이 먼저
    assert sig[1]["info"] is True and "(참고·미검증)" in sig[1]["text"] and "4.0" not in sig[1]["text"]


def test_event_warnings_dedupe_by_type():
    w = us.event_warnings([_ev("유상증자", "악재"), _ev("유상증자", "악재"), _ev("메자닌발행", "악재"),
                           _ev("자사주취득", "호재")])
    assert len(w) == 2 and "유상증자" in w[0]


def _scored_row(events, eps=1000.0):
    d = us.parse_integration({"totalInfos": [
        {"code": "eps", "value": f"{eps:,.0f}원"}, {"code": "bps", "value": "5,000원"},
        {"code": "cnsPer", "value": "5.0배"}, {"code": "pbr", "value": "0.5배"},
        {"code": "highPriceOf52Weeks", "value": "20,000"}], "industryCode": "1",
        "consensusInfo": {"priceTargetMean": "20,000", "recommMean": "4.0"}, "dealTrendInfos": []})
    d["events"] = events
    return {"code": "111111", "name": "A", "market": "KOSPI", "close": 10000.0, "chg_pct": 0.0, "volume": 1.0,
            "value_eok": 1.0, "mcap_eok": 5000.0, "detail": d}


def test_severe_negative_event_blocks_pick_but_keeps_warning():
    scored = us.score_all([_scored_row([_ev("횡령·배임·회계위반", "악재", "")])])
    assert scored[0]["blocked"] is True and scored[0]["warnings"]
    assert us.pick_from(scored) == []


def test_mild_negative_event_warns_without_blocking():
    scored = us.score_all([_scored_row([_ev("유상증자", "악재")])])
    assert scored[0]["blocked"] is False and scored[0]["warnings"]
    assert len(us.pick_from(scored)) == 1


def test_validated_event_adds_event_group_but_unvalidated_does_not():
    base = us.score_all([_scored_row([])])[0]
    boosted = us.score_all([_scored_row([_ev("자사주취득", "호재")])])[0]
    assert boosted["score"] == base["score"] + 1
    contract = us.score_all([_scored_row([_ev("공급계약", "호재", "계약금액 500억원", 19.2)])])[0]
    assert contract["score"] == base["score"]                                     # 미검증 사건은 점수에 영향 없음
    assert any(s["key"] == "공시호재" and s.get("info") for s in contract["signals"])


def test_loss_making_stock_gets_no_event_signal():
    assert us.score_all([_scored_row([_ev("자사주취득", "호재")], eps=-5.0)])[0]["signals"] == []


# ── 잡 ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("stats,status", [
    ({"ok": False, "errors": ["x"], "listed": 0, "relevant": 0, "new": 0, "saved": 0, "doc_try": 0, "doc_fail": 0}, "fail"),
    ({"ok": True, "errors": [], "listed": 0, "relevant": 0, "new": 0, "saved": 0, "doc_try": 0, "doc_fail": 0}, "empty"),
    ({"ok": True, "errors": [], "listed": 500, "relevant": 80, "new": 40, "saved": 40, "doc_try": 10, "doc_fail": 8}, "fail"),
    ({"ok": True, "errors": [], "listed": 500, "relevant": 80, "new": 40, "saved": 40, "doc_try": 10, "doc_fail": 1}, "success"),
    # 2026-10-26: 본문 시도 1건이 실패하자 0/1 → 100% 실패로 오탐. 표본 4건 미만은 판정하지 않는다
    ({"ok": True, "errors": [], "listed": 500, "relevant": 80, "new": 40, "saved": 40, "doc_try": 1, "doc_fail": 1}, "success"),
])
def test_run_daily_job_status(monkeypatch, stats, status):
    monkeypatch.setattr(de, "collect", lambda days=3: stats)
    assert de.run_daily_job()[0] == status


def test_scheduler_job_skips_non_trading_day_and_survives_errors(monkeypatch):
    import scheduler
    rec = []
    monkeypatch.setattr("services.job_ledger.record_job", lambda n, s, d="": rec.append((n, s)))
    monkeypatch.setattr(scheduler, "is_krx_trading_day", lambda: False)
    scheduler.job_dart_events()
    monkeypatch.setattr(scheduler, "is_krx_trading_day", lambda: True)
    monkeypatch.setattr(de, "run_daily_job", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    scheduler.job_dart_events()
    assert rec == [("dart_events", "skipped"), ("dart_events", "fail")]


def test_job_in_ledger_every_weekday():
    from services import job_ledger
    for wd in range(5):
        assert "dart_events" in job_ledger._EXPECTED_BY_WEEKDAY[wd]


def test_save_and_query_roundtrip():
    from db.database import get_conn, init_db
    from sqlalchemy import text
    init_db()
    with get_conn() as c:
        c.execute(text("DELETE FROM dart_events WHERE rcept_no LIKE 'T%'"))
    ev = {"rcept_no": "T1", "date": "2099-01-01", "stock_code": "999999", "corp_name": "T", "market": "KOSPI",
          "report_nm": "x", "event_type": "유상증자", "polarity": "악재", "summary": "", "metric": None}
    assert de.save_events([ev]) == 1
    de.save_events([ev])                                         # 중복 접수번호는 무시돼야 한다
    got = [e for e in de.recent_events(days=100000) if e["stock_code"] == "999999"]
    assert len(got) == 1 and got[0]["event_type"] == "유상증자"
    assert de.by_code(100000)["999999"][0]["polarity"] == "악재"
    with get_conn() as c:
        c.execute(text("DELETE FROM dart_events WHERE rcept_no LIKE 'T%'"))


def test_earnings_without_unit_omits_amount():
    toks = [t for t in _earn(("6,872", "6,120", "12.3", "-", "2,054", "234.6", "-")) if not t.startswith("단위")]
    s, m = de.summarize_earnings(de.parse_earnings(toks))
    assert "6,872" not in s and "+234.6%" in s and m == 234.6


def test_stored_good_earnings_are_read_as_neutral(monkeypatch):
    """극성 규칙을 바꾸기 전에 '호재'로 저장된 잠정실적도 읽을 때 중립으로 보정된다."""
    from db.database import get_conn, init_db
    from sqlalchemy import text
    init_db()
    with get_conn() as c:
        c.execute(text("DELETE FROM dart_events WHERE rcept_no LIKE 'EP%'"))
        for no, typ, pol in (("EP1", "잠정실적", "호재"), ("EP2", "자사주취득", "호재"), ("EP3", "잠정실적", "악재")):
            c.execute(text("INSERT INTO dart_events (rcept_no, date, stock_code, corp_name, market, report_nm, event_type, polarity, "
                           "summary, metric) VALUES (:n, :d, '999998', 'T', 'KOSPI', 'x', :t, :p, '', NULL)"),
                      {"n": no, "d": __import__("datetime").date.today().isoformat(), "t": typ, "p": pol})
    got = {e["event_type"] + ":" + e["polarity"] for e in de.recent_events(days=3) if e["stock_code"] == "999998"}
    with get_conn() as c:
        c.execute(text("DELETE FROM dart_events WHERE rcept_no LIKE 'EP%'"))
    assert got == {"잠정실적:중립", "자사주취득:호재", "잠정실적:악재"}


def test_buyback_cancel_and_dividend_are_not_validated():
    """자사주소각·배당결정은 검증 표본이 0건(DART 주요사항보고서 목록에 없는 유형)이라 ✔를 달 수 없다 — 참고(미검증)로만."""
    sig = us.event_signals([{"event_type": "자사주소각", "polarity": "호재", "summary": "", "metric": None, "date": "2026-10-21"},
                            {"event_type": "배당결정", "polarity": "호재", "summary": "", "metric": None, "date": "2026-10-21"}])
    assert len(sig) == 2 and all(x.get("info") and not x.get("validated") for x in sig)
    assert us.score(sig) == 0 and us.validated_count(sig) == 0
