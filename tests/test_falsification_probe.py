"""반증 발동 자율 조사 — 형식·숫자 근거 검증, 기록, CEO 입력 주입 (2026-10-28)."""
from db.database import init_db, get_conn
from sqlalchemy import text
import services.falsification_probe as fp
import services.falsification_monitor as fm

H = {"code": "000660", "name": "SK하이닉스", "avg_price": 1935965, "price": 1290000}
FIRED = ["1,300,000원 하회 (현재 1,290,000.00)"]


def _clear():
    init_db()
    with get_conn() as conn:
        conn.execute(text("DELETE FROM system_settings WHERE key='falsify.probe'"))


def test_validate_requires_hypothesis_prefix_and_grounded_numbers():
    facts = fp.build_facts(H, FIRED, {"kospi_chg": -3.2, "vix": 25.1}, ["외국인 순매도 확대"])
    assert fp.validate("가설: 시장 전체 하락(KOSPI -3.2%)에 동행한 것으로 보임", facts)
    assert fp.validate("시장 하락 때문", facts) is None                     # '가설:' 없음
    assert fp.validate("가설: 반도체 수요가 47% 급감", facts) is None       # 입력에 없는 숫자


def test_run_probe_records_and_injects_into_status_context(monkeypatch):
    _clear()
    why = fp.run_probe(H, FIRED, chat_fn=lambda s, u, max_tokens=0: "가설: 종목 고유 문제보다 시장 전체 약세 영향으로 보임")
    assert why and fp.recent_for("000660") == why
    monkeypatch.setattr(fm, "_load_holdings_with_falsification",
                        lambda: [{"code": "000660", "name": "SK하이닉스", "avg_price": 1935965,
                                  "falsification": "종가 1,300,000원 하회 시 재검토"}])
    ctx = fm.build_status_context({"000660": 1290000.0}, {})
    assert "반증 발동" in ctx and "AI 조사" in ctx and "시장 전체 약세" in ctx


def test_probe_failure_never_raises_and_records_nothing():
    _clear()

    def boom(*a, **k):
        raise RuntimeError("llm down")

    assert fp.run_probe(H, FIRED, chat_fn=boom) is None
    assert fp.run_probe(H, FIRED, chat_fn=lambda *a, **k: "가설: 숫자 99% 창작") is None
    assert fp.recent_for("000660") == ""


def test_keeps_latest_per_code_and_caps_history():
    _clear()
    for i in range(12):
        fp.run_probe({"code": f"C{i:02d}", "name": "x"}, ["a"], chat_fn=lambda *a, **k: "가설: 근거 부족")
    fp.run_probe({"code": "C11", "name": "x"}, ["a"], chat_fn=lambda *a, **k: "가설: 근거 부족 재조사")
    items = fp._load()
    assert len(items) == fp._KEEP and [p["code"] for p in items].count("C11") == 1
