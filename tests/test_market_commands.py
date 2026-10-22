"""/ideas /sector /why 명령 + stock_explain 회귀 테스트 — 코드 출력만, 기록이 없으면 솔직히 없다고 말한다."""
import pytest
from sqlalchemy import text

from clients import telegram_advisor_cmds as cmds
from clients.telegram_aliases import resolve
from db.database import get_conn, init_db
from services import stock_explain as se


@pytest.fixture
def db():
    init_db()
    tables = ("universe_screen_daily", "us_screen_daily", "dart_events", "industry_daily")
    with get_conn() as c:
        for t in tables:
            c.execute(text(f"DELETE FROM {t}"))
    yield
    with get_conn() as c:
        for t in tables:
            c.execute(text(f"DELETE FROM {t}"))


def _kr(date, code="005930", score=4, signals="수익성,저평가,고점대비낙폭"):
    with get_conn() as c:
        c.execute(text(
            "INSERT INTO universe_screen_daily (date, code, name, market, close, chg_pct, mcap_eok, per, pbr, cns_per, eps, target, "
            "high52, score, signals) VALUES (:d, :c, '삼성전자', 'KOSPI', 10000, 1.0, 5000, 8, 0.8, 6.0, 1000, 15000, 20000, :s, :g)"),
                  {"d": date, "c": code, "s": score, "g": signals})


def _us(date, code="NVDA", score=4):
    with get_conn() as c:
        c.execute(text(
            "INSERT INTO us_screen_daily (date, code, name, sector, close, chg_pct, mcap_usd, fwd_pe, roe, target, high52, score, "
            "signals) VALUES (:d, :c, 'NVIDIA', 'Tech', 100, 1.0, 5e9, 12, 0.2, 130, 200, :s, '수익성,내부자매수')"),
                  {"d": date, "c": code, "s": score})


def test_why_kr_shows_signals_numbers_and_history(db):
    _kr("2026-10-21", score=3)
    _kr("2026-10-22", score=4)
    out = se.explain("005930")
    assert "삼성전자(005930)" in out and "신호 4묶음" in out and "3묶음 이상 = 후보" in out
    assert "목표가 대비 +50%" in out and "52주 고점 대비 -50%" in out                 # 코드 계산
    assert "수익성: 자기자본 대비 이익이 높음" in out
    assert "10-22 4 ← 10-21 3" in out


def test_why_kr_below_threshold_says_not_a_candidate(db):
    _kr("2026-10-22", score=2)
    assert "3묶음 미만 = 후보 아님" in se.explain("005930")


def test_why_kr_includes_recent_disclosures(db):
    _kr("2026-10-22")
    with get_conn() as c:
        c.execute(text("INSERT INTO dart_events (rcept_no, date, stock_code, corp_name, market, report_nm, event_type, polarity, "
                       "summary, metric) VALUES ('W1', :d, '005930', '삼성전자', 'KOSPI', 'x', '자사주취득', '호재', '', NULL)"),
                  {"d": __import__("datetime").date.today().isoformat()})
    out = se.explain("005930")
    assert "최근 2주 공시" in out and "자사주취득" in out


def test_why_unknown_stock_does_not_invent(db):
    out = se.explain("999999")
    assert "기록에 없습니다" in out and "신호" not in out.split("기록에 없습니다")[0]


def test_why_us_ticker(db):
    _us("2026-10-22")
    out = se.explain("nvda")
    assert "NVIDIA(NVDA)" in out and "내부자매수: 임원·이사가 최근 공개시장에서 매수" in out and "SEC" in out


def test_why_us_unknown_ticker(db):
    assert "기록에 없습니다" in se.explain("ZZZZ")


def test_why_name_resolution_uses_resolver(db, monkeypatch):
    _kr("2026-10-22")
    import services.research_service as rs
    monkeypatch.setattr(rs, "resolve_code", lambda q: ("005930", "삼성전자") if q == "삼성전자" else ("", ""))
    assert "삼성전자(005930)" in se.explain("삼성전자")
    assert "찾지 못했습니다" in se.explain("없는회사이름")


def test_why_usage_when_empty():
    assert "사용법" in se.explain("")


def test_why_survives_db_error(monkeypatch):
    monkeypatch.setattr(se, "_q", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    assert "오류" in se.explain("005930")


def test_commands_send_code_output(db, monkeypatch):
    sent = []
    send = lambda chat, msg: sent.append(msg)
    import services.portfolio_service as ps
    monkeypatch.setattr(ps, "get_portfolio", lambda: [{"code": "000660"}])
    cmds.cmd_ideas("1", "", send)
    assert "기록이 없거나" in sent[-1]                                         # 빈 DB → 솔직한 안내
    cmds.cmd_sector("1", "", send)
    assert "업종 온도계 기록이 없습니다" in sent[-1]
    _kr("2026-10-22")
    cmds.cmd_why("1", "005930", send)
    assert "삼성전자(005930)" in sent[-1]


def test_ideas_for_user_has_no_llm_instruction_header(db, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from services import market_ideas
    today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")
    _kr(today)
    user = market_ideas.build_block(set(), for_user=True)
    llm = market_ideas.build_block(set(), for_user=False)
    assert "/why" in user and "인용하고" not in user          # 사용자에겐 LLM 지시문을 보이지 않는다
    assert "인용하고" in llm


def test_aliases_resolve_to_new_commands():
    assert resolve("후보") == "/ideas" and resolve("/업종") == "/sector"
    assert resolve("왜 삼성전자") == "/why 삼성전자"


def test_normal_chat_with_similar_words_untouched():
    for t in ("후보가 뭐야?", "업종별로 알려줘", "왜냐하면 그래서"):
        assert resolve(t) == t


def test_menu_keyboard_and_handlers_consistent():
    from clients.telegram_aliases import ALIASES, MENU_KEYBOARD
    from clients.telegram_bot import _HANDLERS
    for cmd in ("/ideas", "/sector", "/why"):
        assert cmd in _HANDLERS
    assert all(resolve(b) in _HANDLERS for row in MENU_KEYBOARD for b in row)
    assert all(c in _HANDLERS for c in ALIASES.values())
