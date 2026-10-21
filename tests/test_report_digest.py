"""증권사 리포트 수집·집계 회귀 테스트 (2026-10-21).

네이버가 리서치 페이지를 이전해 HTML 표 수집이 에러 없이 0건이 됐던 사고,
그리고 리포트를 '결론'이 아닌 '참고'로 쓰기 위한 코드 집계(증권사 수 중복 제거 등).
"""
import clients.securities_report_client as src
from services import report_digest as rd


def _rep(rtype, firm, title, date="2026-10-21", link=None):
    return {"type": rtype, "firm": firm, "title": title, "stock": "", "date": date,
            "link": link or f"https://x/{firm}/{title}", "summary": ""}


# ── 수집기 ─────────────────────────────────────────────────────────

def test_normalize_maps_json_fields():
    item = {"title": "10/21 Morning Letter", "brokerName": "키움증권", "writeDate": "2026-10-21",
            "endUrl": "https://m.stock.naver.com/research/economy/1", "itemName": "", "itemCode": ""}
    r = src._normalize(item, "경제분석")
    assert r["firm"] == "키움증권" and r["type"] == "경제분석" and r["date"] == "2026-10-21"


def test_normalize_drops_incomplete_items():
    assert src._normalize({"title": "", "brokerName": "x"}, "경제분석") is None
    assert src._normalize({"title": "t", "brokerName": ""}, "경제분석") is None


def test_unknown_or_old_date_is_not_recent():
    assert not src._is_recent("")
    assert not src._is_recent("2020-01-01")


def test_fetch_failure_returns_empty_not_exception(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(src.requests, "get", boom)
    assert src.fetch_securities_reports() == []


def test_api_failure_flag_returns_empty(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self): return {"isSuccess": False, "message": "x"}
    monkeypatch.setattr(src.requests, "get", lambda *a, **k: R())
    assert src.fetch_securities_reports() == []


# ── 분류 ───────────────────────────────────────────────────────────

def test_theme_and_tone_classification():
    assert "반도체·AI" in rd.classify_themes("HBM 수요 둔화 우려")
    assert rd.classify_tone("HBM 수요 둔화 우려") == "경계"
    assert rd.classify_tone("실적 상향 기대, 그러나 변동성 우려") == "혼재"
    assert rd.classify_tone("10/21 Morning Letter") == "중립"


# ── 집계: 같은 증권사 중복은 1곳 ──────────────────────────────────

def test_same_firm_counted_once_per_theme():
    reps = [
        _rep("경제분석", "A증권", "금리 인하 기대"),
        _rep("투자전략", "A증권", "금리 경로 점검"),
        _rep("경제분석", "B증권", "금리 동결 전망"),
        _rep("종목분석", "C증권", "삼성전자 금리 무관"),   # 종목분석은 매크로 집계 제외
    ]
    s = rd.summarize_today(reps)
    assert s["theme_firms"]["금리·통화정책"] == 2
    assert s["n_firms"] == 2


def test_digest_contains_bias_warning_and_no_stock_reports():
    d = rd.build_digest([_rep("경제분석", "A증권", "환율 급등 우려")])
    assert "환율·달러" in d and "낙관 편향" in d and "만들어내지 말 것" in d


def test_digest_empty_when_only_stock_reports():
    assert rd.build_digest([_rep("종목분석", "A증권", "목표가 상향")]) == ""


# ── 수집 상태 감지 (조용한 0건 방지) ─────────────────────────────

def test_assess_flags_zero_reports():
    ok, why = rd.assess_collection([])
    assert not ok and "0건" in why


def test_assess_flags_only_stock_reports():
    ok, _ = rd.assess_collection([_rep("종목분석", "A증권", "t")])
    assert not ok


def test_assess_ok_with_macro():
    assert rd.assess_collection([_rep("경제분석", "A증권", "t")])[0]


def test_record_health_writes_branch_fail(monkeypatch):
    calls = []
    import services.job_ledger as jl
    monkeypatch.setattr(jl, "record_job", lambda n, s, d="": calls.append((n, s)))
    rd.record_collection_health([])
    assert calls == [("branch:report_sources", "fail")]


# ── 보관(중복 방지) ──────────────────────────────────────────────

def test_store_reports_dedups():
    from db.database import init_db
    init_db()
    reps = [_rep("경제분석", "TEST_FIRM_DEDUP", "중복 테스트 리포트", link="https://test/dedup/1")]
    rd.store_reports(reps, source="test")
    assert rd.store_reports(reps, source="test") == 0


# ── 소스 목록 ────────────────────────────────────────────────────

def test_dead_sources_removed_and_official_sources_present():
    from clients.intelligence_client import INTELLIGENCE_SOURCES
    names = {s["name"] for v in INTELLIGENCE_SOURCES.values() for s in v}
    assert not ({"Reuters Markets", "Barrons", "Korea Herald Biz"} & names)
    assert {"한국은행", "Fed 통화정책", "ECB"} <= names
