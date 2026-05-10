"""
전일 미국 섹터 강세 → 오늘 한국 수혜 종목 발굴
"""
import logging
from graph.state import InvestmentState
from clients.openai_client import chat

logger = logging.getLogger(__name__)

# 미국 섹터 강세 → 한국 수혜 종목 매핑
# strength: 높음(직접 공급망/경쟁) / 보통(간접 연관) / 낮음(테마적 연관)
US_SECTOR_TO_KR: dict[str, list[dict]] = {
    "반도체": [
        {"name": "SK하이닉스",   "code": "000660", "reason": "HBM 최대 공급사",         "strength": "높음"},
        {"name": "삼성전자",     "code": "005930", "reason": "HBM·파운드리 수혜",        "strength": "높음"},
        {"name": "한미반도체",   "code": "042700", "reason": "HBM 패키징 장비 공급",     "strength": "높음"},
        {"name": "리노공업",     "code": "058470", "reason": "반도체 테스트 소켓",        "strength": "보통"},
        {"name": "원익IPS",      "code": "240810", "reason": "반도체 증착·식각 장비",     "strength": "보통"},
        {"name": "HPSP",         "code": "403870", "reason": "고압 어닐링 장비 독점",     "strength": "보통"},
    ],
    "기술/IT": [
        {"name": "삼성전자",     "code": "005930", "reason": "스마트기기 부품 수요",      "strength": "보통"},
        {"name": "LG이노텍",     "code": "011070", "reason": "카메라 모듈 공급망",        "strength": "보통"},
        {"name": "삼성전기",     "code": "009150", "reason": "MLCC·기판 공급",            "strength": "보통"},
    ],
    "전기차": [
        {"name": "LG에너지솔루션", "code": "373220", "reason": "글로벌 배터리 최대 공급사", "strength": "높음"},
        {"name": "삼성SDI",       "code": "006400", "reason": "배터리 셀 공급사",           "strength": "높음"},
        {"name": "에코프로비엠",  "code": "247540", "reason": "양극재 공급사",              "strength": "높음"},
        {"name": "포스코퓨처엠",  "code": "003670", "reason": "양극재·소재 공급",           "strength": "보통"},
        {"name": "엘앤에프",      "code": "066970", "reason": "양극재 공급사",              "strength": "보통"},
    ],
    "방산": [
        {"name": "한화에어로스페이스", "code": "012450", "reason": "K9 수출·방산 성장",  "strength": "높음"},
        {"name": "LIG넥스원",         "code": "079550", "reason": "미사일·방산 수출",    "strength": "높음"},
        {"name": "현대로템",           "code": "064350", "reason": "K2 전차 수출",        "strength": "보통"},
        {"name": "한국항공우주",       "code": "047810", "reason": "항공기·드론 수출",    "strength": "보통"},
    ],
    "바이오": [
        {"name": "삼성바이오로직스", "code": "207940", "reason": "글로벌 CMO 수혜",      "strength": "높음"},
        {"name": "셀트리온",         "code": "068270", "reason": "바이오시밀러 글로벌",  "strength": "보통"},
        {"name": "유한양행",         "code": "000100", "reason": "신약 파이프라인",       "strength": "낮음"},
    ],
    "금융": [
        {"name": "KB금융",     "code": "105560", "reason": "글로벌 금리 연동",         "strength": "보통"},
        {"name": "신한지주",   "code": "055550", "reason": "글로벌 금리 연동",         "strength": "보통"},
        {"name": "삼성생명",   "code": "032830", "reason": "금리 수혜 보험사",         "strength": "보통"},
    ],
    "에너지": [
        {"name": "한국전력",       "code": "015760", "reason": "에너지 가격 연동",      "strength": "보통"},
        {"name": "두산에너빌리티", "code": "034020", "reason": "원전·에너지 기자재",    "strength": "보통"},
        {"name": "한전KPS",        "code": "051600", "reason": "발전설비 유지보수",     "strength": "낮음"},
    ],
    "로봇/AI": [
        {"name": "레인보우로보틱스", "code": "277810", "reason": "산업용 로봇 국내 1위", "strength": "높음"},
        {"name": "두산로보틱스",    "code": "454910", "reason": "협동로봇 성장",          "strength": "높음"},
        {"name": "HD현대",          "code": "267250", "reason": "로봇·AI 사업 보유",      "strength": "보통"},
        {"name": "현대로템",        "code": "064350", "reason": "로봇·방산 융합",          "strength": "보통"},
    ],
    "자동차": [
        {"name": "현대차",    "code": "005380", "reason": "글로벌 자동차 동반 상승",   "strength": "높음"},
        {"name": "기아",      "code": "000270", "reason": "글로벌 자동차 동반 상승",   "strength": "높음"},
        {"name": "현대모비스", "code": "012330", "reason": "자동차 부품 공급망",        "strength": "보통"},
        {"name": "만도",      "code": "204320", "reason": "자동차 부품 공급망",         "strength": "보통"},
    ],
    "소프트웨어": [
        {"name": "NAVER",  "code": "035420", "reason": "AI·플랫폼 섹터 동반",        "strength": "낮음"},
        {"name": "카카오",  "code": "035720", "reason": "AI·플랫폼 섹터 동반",        "strength": "낮음"},
    ],
}

_SYSTEM = """당신은 미국 시장 → 한국 주식 영향 분석 전문가입니다.
전일 미국 섹터 ETF 성과를 바탕으로 오늘 한국 시장 수혜 종목을 발굴하세요.

분석 항목:
1. 미국 강세 섹터 TOP3 → 한국 수혜 종목 구체화 (코드 포함)
2. 미국 약세 섹터 → 한국 리스크 종목 경고
3. 52주 신고가 미국 종목의 한국 파급효과
4. 오늘 장전 전략: 어느 섹터의 어떤 종목을 먼저 볼 것인가

출력: "[미국발 오늘 주목 한국 종목]" 헤더로 시작, 간결하게, 이모지 활용"""


def run(state: InvestmentState) -> InvestmentState:
    try:
        sectors = state.get("us_sector_data", {})
        highs   = state.get("us_52w_highs", [])

        if not sectors:
            state["us_impact_report"] = "미국 섹터 데이터 없음"
            return state

        # 섹터별 등락률 정렬
        sorted_sectors = sorted(sectors.items(), key=lambda x: x[1].get("change_pct", 0), reverse=True)
        strong = [(s, d) for s, d in sorted_sectors if d.get("change_pct", 0) > 0.3]
        weak   = [(s, d) for s, d in sorted_sectors if d.get("change_pct", 0) < -0.3]

        # 강세 섹터 → 한국 종목 매핑 (높음 우선)
        mapped_lines: list[str] = []
        for sector, data in strong[:4]:
            kr_stocks = US_SECTOR_TO_KR.get(sector, [])
            high_conf = [s for s in kr_stocks if s["strength"] == "높음"][:3]
            if high_conf:
                stocks_str = ", ".join(f"{s['name']}({s['code']})" for s in high_conf)
                mapped_lines.append(f"▲ {sector} {data['change_pct']:+.1f}% → {stocks_str}")

        # AI 입력 컨텍스트
        sector_text = "\n".join(
            f"  {s}: {d.get('change_pct', 0):+.2f}% ({d.get('symbol', '')})"
            for s, d in sorted_sectors
        )
        highs_text = "\n".join(
            f"  {h['name']}({h['ticker']}): 고점 대비 {h['pct_from_high']:+.1f}%  전일 {h['change_pct']:+.1f}%"
            for h in highs[:5]
        ) or "없음"
        mapped_text = "\n".join(mapped_lines) or "강세 섹터 없음 (약보합 장세)"

        context = (
            f"미국 섹터별 전일 등락률:\n{sector_text}\n\n"
            f"강세 섹터 → 한국 주목 종목 사전 매핑:\n{mapped_text}\n\n"
            f"52주 신고가 근접 미국 종목:\n{highs_text}"
        )

        result = chat(_SYSTEM, context, max_tokens=1200)
        state["us_impact_report"] = result
        logger.info("[미국영향팀] 완료 — 강세 %d개 약세 %d개 섹터", len(strong), len(weak))
    except Exception as e:
        logger.error("[미국영향팀] 실패: %s", e)
        state["us_impact_report"] = "분석 실패"
        state["errors"].append(f"us_impact_agent: {e}")
    return state
