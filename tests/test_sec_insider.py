"""SEC 내부자 거래 클라이언트·스크리너 신호 회귀 테스트.

실측 중 잡은 함정: ① 'N/A'를 쪼갠 뒤 검사해 가짜 종목 'N'·'A'가 11명 매수로 집계됨(→ 심볼이 아니라 발행사 CIK로 집계)
② 10% 대주주의 대형 지분거래가 임원 매수처럼 섞임(→ 임원·이사만 신호, 대주주는 holder_value로 분리)
③ 사전 매도계획(10b5-1) 거래·공개시장 매수(P)가 아닌 거래(수여 A·행사 M·증여 G)가 섞이면 안 됨.
"""
import io
import zipfile
from datetime import date

import pytest

from clients import sec_insider_client as si
from services import us_universe_screener as us

_SUB_H = "ACCESSION_NUMBER\tFILING_DATE\tPERIOD_OF_REPORT\tDOCUMENT_TYPE\tISSUERCIK\tISSUERNAME\tISSUERTRADINGSYMBOL\tAFF10B5ONE"
_OWN_H = "ACCESSION_NUMBER\tRPTOWNERCIK\tRPTOWNERNAME\tRPTOWNER_RELATIONSHIP\tRPTOWNER_TITLE"
_TR_H = ("ACCESSION_NUMBER\tNONDERIV_TRANS_SK\tTRANS_DATE\tTRANS_CODE\tTRANS_SHARES\tTRANS_PRICEPERSHARE\t"
         "TRANS_ACQUIRED_DISP_CD")


def _zip(subs, owners, trans):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SUBMISSION.tsv", "\n".join([_SUB_H] + ["\t".join(map(str, r)) for r in subs]))
        z.writestr("REPORTINGOWNER.tsv", "\n".join([_OWN_H] + ["\t".join(map(str, r)) for r in owners]))
        z.writestr("NONDERIV_TRANS.tsv", "\n".join([_TR_H] + ["\t".join(map(str, r)) for r in trans]))
    return buf.getvalue()


def _sub(acc, cik=1000, sym="AAA", plan=0, doc="4", filed="05-SEP-2026"):
    return (acc, filed, "03-SEP-2026", doc, f"{cik:010d}", "Alpha", sym, plan)


def _own(acc, who, rel="Officer", title="CEO"):
    return (acc, who, f"Person{who}", rel, title)


def _tr(acc, code="P", shares=1000, price=50.0, ad="A", d="03-SEP-2026"):
    return (acc, 1, d, code, shares, price, ad)


def test_parse_zip_keeps_only_open_market_buys():
    z = _zip([_sub("a1"), _sub("a2"), _sub("a3"), _sub("a4", doc="3")],
             [_own("a1", 1), _own("a2", 2), _own("a3", 3), _own("a4", 4)],
             [_tr("a1", "P"), _tr("a2", "A"), _tr("a3", "S", ad="D"), _tr("a4", "P")])
    rows = si.parse_zip(z)
    assert len(rows) == 1 and rows[0]["cik"] == 1000 and rows[0]["value"] == 50000.0


def test_parse_zip_skips_issuer_without_cik_and_bad_numbers():
    z = _zip([_sub("a1", cik=0), _sub("a2")], [_own("a1", 1), _own("a2", 2)],
             [_tr("a1"), _tr("a2", shares="x"), _tr("a2", price=0)])
    assert si.parse_zip(z) == []


def _patch_fetch(monkeypatch, content):
    import requests

    class R:
        def __init__(self, c): self.content, self.text = c, ""
        def raise_for_status(self): pass
    monkeypatch.setattr(si, "latest_zip_urls", lambda n=2: ["https://x/2026q3_form345.zip"])
    monkeypatch.setattr(requests, "get", lambda *a, **k: R(content))
    si._CACHE.update(ts=0.0, key=None, val=None)


def test_aggregates_officers_by_cik_and_separates_holders(monkeypatch):
    z = _zip([_sub("a1"), _sub("a2"), _sub("a3"), _sub("a4", plan=1), _sub("a5", cik=2000, sym="NONE")],
             [_own("a1", 1), _own("a2", 2, "Director", ""), _own("a3", 3, "TenPercentOwner", ""),
              _own("a4", 4), _own("a5", 5)],
             [_tr("a1"), _tr("a2"), _tr("a3", shares=1_000_000), _tr("a4"), _tr("a5")])
    _patch_fetch(monkeypatch, z)
    v = si.fetch_recent_purchases(days=90, today=date(2026, 10, 22))
    a = v["by_cik"][1000]
    assert a["buyers"] == 2 and a["value"] == 100000.0          # 임원+이사만, 10b5-1 계획 거래 제외
    assert a["holder_value"] == 50_000_000.0                     # 대주주는 분리
    assert 2000 in v["by_cik"] and v["data_through"] == date(2026, 9, 5)


def test_window_excludes_old_trades(monkeypatch):
    z = _zip([_sub("a1")], [_own("a1", 1)], [_tr("a1", d="01-MAR-2026")])
    _patch_fetch(monkeypatch, z)
    v = si.fetch_recent_purchases(days=90, today=date(2026, 10, 22))
    assert v["ok"] and v["by_cik"] == {}


def test_failure_returns_not_ok(monkeypatch):
    monkeypatch.setattr(si, "latest_zip_urls", lambda n=2: [])
    si._CACHE.update(ts=0.0, key=None, val=None)
    assert si.fetch_recent_purchases()["ok"] is False


def test_latest_zip_urls_picks_newest_regardless_of_folder(monkeypatch):
    import requests

    class R:
        text = ('<a href="/files/datastandardsinnovation/data/insider-transactions-data-sets/2026q3_form345.zip">'
                '<a href="/files/structureddata/data/insider-transactions-data-sets/2026q1_form345.zip">'
                '<a href="/files/datastandardsinnovation/data/insider-transactions-data-sets/2026q2_form345.zip">')
        def raise_for_status(self): pass
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    urls = si.latest_zip_urls(2)
    assert [u.rsplit("/", 1)[-1] for u in urls] == ["2026q3_form345.zip", "2026q2_form345.zip"]


# ── 스크리너 신호 ──────────────────────────────────────────────────────────

_E = {"eps": 5.0, "ni": 200.0, "equity": 1000.0, "revenue": 1300.0, "liabilities": 2000.0}
_P = {"close": 100.0, "high52": 200.0, "ma50": 90.0, "ma200": 110.0}


def _row(ins):
    row = {"code": "AAA", "name": "A", "mcap_usd": 5e9, "chg_pct": 0.0, "sector": "Tech", "industry": "Software"}
    return {**row, "detail": us.build_detail(row, _E, _P, ins)}


@pytest.mark.parametrize("ins,expect", [
    ({"buyers": 2, "value": 150_000.0, "last": date(2026, 9, 5)}, True),     # 2명+10만$
    ({"buyers": 1, "value": 600_000.0, "last": date(2026, 9, 5)}, True),     # 1명이라도 50만$+
    ({"buyers": 1, "value": 90_000.0, "last": date(2026, 9, 5)}, False),     # 소액 단독
    ({"buyers": 2, "value": 50_000.0, "last": date(2026, 9, 5)}, False),     # 여러 명이라도 소액
    ({"buyers": 0, "value": 0.0, "last": None, "holder_value": 9e9}, False), # 대주주만
    (None, False),
])
def test_insider_signal_thresholds(ins, expect):
    sig, _ = us.compute_signals(_row(ins), {})
    assert ("내부자매수" in {s["key"] for s in sig}) is expect


def test_insider_is_info_only_and_does_not_add_to_score():
    """검증(scripts/backtest_event_signals.py insider): 임원·이사 매수 군집 이후 초과수익 없음(강한 군집 60일 t=-3.8) → 점수 제외, 참고 표시만."""
    with_ins, _ = us.compute_signals(_row({"buyers": 3, "value": 300_000.0, "last": date(2026, 9, 5)}), {})
    without, _ = us.compute_signals(_row(None), {})
    ins = [s for s in with_ins if s["key"] == "내부자매수"]
    assert len(ins) == 1 and ins[0]["info"] is True and ins[0]["text"].startswith("(참고)")
    assert us.score(with_ins) == us.score(without)


def test_saved_signal_string_marks_info_signals():
    """저장되는 신호 문자열에서 참고용은 '(참고)'로 구분돼 점수 신호처럼 보이지 않는다."""
    from services import stock_explain
    r = _row({"buyers": 3, "value": 300_000.0, "last": date(2026, 9, 5)})
    sig, warn = us.compute_signals(r, {})
    rec = {**r, "signals": sig, "warnings": warn, "score": us.score(sig)}
    from db.database import get_conn, init_db
    from sqlalchemy import text
    init_db()
    with get_conn() as c:
        c.execute(text("DELETE FROM us_screen_daily"))
    us.save_daily({"scored": [rec]}, date="2026-10-22")
    with get_conn() as c:
        saved = c.execute(text("SELECT signals FROM us_screen_daily")).scalar()
        c.execute(text("DELETE FROM us_screen_daily"))
    assert "내부자매수(참고)" in saved
    assert "참고용" in "\n".join(stock_explain._signal_lines(saved))
