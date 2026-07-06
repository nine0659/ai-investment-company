"""주간 추천 파서 — 추천→추적 학습 루프의 입구 + 환각 차단 최종 관문.

브리핑 텍스트에서 추천을 파싱해 추적 DB에 넣는 지점은 LLM 환각이
'기록'으로 굳어질 수 있는 유일한 경로다. 실데이터 교차검증이 무너지면
존재하지 않는 종목·가격이 성과 통계를 오염시킨다.
"""
from services.recommendation_service import recs_from_weekly_picks

PRICES = {"005930": 309500, "000660": 2425000, "033780": 175100}

SAMPLE = """💡 한 줄 결론: 반도체 조정을 분할매수 기회로.

1. 삼성전자 (005930) — 현재가 309,500원 → 목표가 370,000원 (상승여력 +19.5%)
   왜: 연간 이익 성장이 견조하고 재무가 탄탄합니다.
   조심할 것: 반도체 업황 변동성.

2. KT&G (033780) (지난 추천 유지)

3. SK하이닉스 (000660) — 현재가 2,425,000원 → 목표가 2,900,000원 (상승여력 +19.6%)
   왜: AI 메모리 수요가 이어집니다.
   조심할 것: 고점 부담.

📌 시장 한 줄 평: 조정 국면이나 우량주 중심 대응 유효.
"""


def test_parses_valid_picks_with_actual_price():
    recs = recs_from_weekly_picks(SAMPLE, PRICES)
    codes = {r["code"] for r in recs}
    assert codes == {"005930", "000660"}
    samsung = next(r for r in recs if r["code"] == "005930")
    assert samsung["entry_price"] == 309500      # 항상 실데이터 가격
    assert samsung["target_price"] == 370000
    assert "이익 성장" in samsung["rationale"]


def test_maintained_pick_not_resaved():
    # "(지난 추천 유지)"는 최초 추천일 기준으로 이미 추적 중 — 재저장 금지
    recs = recs_from_weekly_picks(SAMPLE, PRICES)
    assert all(r["code"] != "033780" for r in recs)


def test_hallucinated_code_dropped():
    fake = "1. 유령전자 (999999) — 현재가 50,000원 → 목표가 70,000원\n   왜: 근거 없음\n"
    recs = recs_from_weekly_picks(fake, PRICES)
    assert recs == []


def test_text_price_mismatch_uses_actual_data():
    # LLM이 현재가를 잘못 써도 진입가는 실데이터로 기록
    wrong = "1. 삼성전자 (005930) — 현재가 999,000원 → 목표가 1,100,000원\n"
    recs = recs_from_weekly_picks(wrong, PRICES)
    # 목표가 1,100,000은 실가격(309,500) 대비 3.5배 → 비현실 목표로 폐기
    assert recs == []

    plausible = "1. 삼성전자 (005930) — 현재가 320,000원 → 목표가 380,000원\n"
    recs2 = recs_from_weekly_picks(plausible, PRICES)
    assert len(recs2) == 1
    assert recs2[0]["entry_price"] == 309500


def test_absurd_target_dropped():
    absurd = "1. 삼성전자 (005930) — 현재가 309,500원 → 목표가 3,000,000원\n"
    assert recs_from_weekly_picks(absurd, PRICES) == []


def test_duplicate_code_saved_once():
    dup = (
        "1. 삼성전자 (005930) — 현재가 309,500원 → 목표가 370,000원\n"
        "2. 삼성전자 (005930) — 현재가 309,500원 → 목표가 350,000원\n"
    )
    assert len(recs_from_weekly_picks(dup, PRICES)) == 1


def test_empty_report_no_crash():
    assert recs_from_weekly_picks("", PRICES) == []
    assert recs_from_weekly_picks("추천 없음", {}) == []
