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
        {"name": "SK하이닉스",   "code": "000660", "market": "KOSPI",  "reason": "HBM 최대 공급사",         "strength": "높음"},
        {"name": "삼성전자",     "code": "005930", "market": "KOSPI",  "reason": "HBM·파운드리 수혜",        "strength": "높음"},
        {"name": "한미반도체",   "code": "042700", "market": "KOSDAQ", "reason": "HBM 패키징 장비 공급",     "strength": "높음"},
        {"name": "리노공업",     "code": "058470", "market": "KOSDAQ", "reason": "반도체 테스트 소켓",        "strength": "보통"},
        {"name": "원익IPS",      "code": "240810", "market": "KOSDAQ", "reason": "반도체 증착·식각 장비",     "strength": "보통"},
        {"name": "HPSP",         "code": "403870", "market": "KOSDAQ", "reason": "고압 어닐링 장비 독점",     "strength": "보통"},
    ],
    "기술/IT": [
        {"name": "삼성전자",     "code": "005930", "market": "KOSPI",  "reason": "스마트기기 부품 수요",      "strength": "보통"},
        {"name": "LG이노텍",     "code": "011070", "market": "KOSPI",  "reason": "카메라 모듈 공급망",        "strength": "보통"},
        {"name": "삼성전기",     "code": "009150", "market": "KOSPI",  "reason": "MLCC·기판 공급",            "strength": "보통"},
    ],
    "전기차": [
        {"name": "LG에너지솔루션", "code": "373220", "market": "KOSPI",  "reason": "글로벌 배터리 최대 공급사", "strength": "높음"},
        {"name": "삼성SDI",        "code": "006400", "market": "KOSPI",  "reason": "배터리 셀 공급사",           "strength": "높음"},
        {"name": "에코프로비엠",   "code": "247540", "market": "KOSDAQ", "reason": "양극재 공급사",              "strength": "높음"},
        {"name": "포스코퓨처엠",   "code": "003670", "market": "KOSPI",  "reason": "양극재·소재 공급",           "strength": "보통"},
        {"name": "엘앤에프",       "code": "066970", "market": "KOSDAQ", "reason": "양극재 공급사",              "strength": "보통"},
    ],
    "방산": [
        {"name": "한화에어로스페이스", "code": "012450", "market": "KOSPI", "reason": "K9 수출·방산 성장",  "strength": "높음"},
        {"name": "LIG넥스원",          "code": "079550", "market": "KOSPI", "reason": "미사일·방산 수출",    "strength": "높음"},
        {"name": "현대로템",            "code": "064350", "market": "KOSPI", "reason": "K2 전차 수출",        "strength": "보통"},
        {"name": "한국항공우주",        "code": "047810", "market": "KOSPI", "reason": "항공기·드론 수출",    "strength": "보통"},
    ],
    "바이오": [
        {"name": "삼성바이오로직스", "code": "207940", "market": "KOSPI", "reason": "글로벌 CMO 수혜",      "strength": "높음"},
        {"name": "셀트리온",         "code": "068270", "market": "KOSPI", "reason": "바이오시밀러 글로벌",  "strength": "보통"},
        {"name": "유한양행",         "code": "000100", "market": "KOSPI", "reason": "신약 파이프라인",       "strength": "낮음"},
    ],
    "금융": [
        {"name": "KB금융",   "code": "105560", "market": "KOSPI", "reason": "글로벌 금리 연동",         "strength": "보통"},
        {"name": "신한지주", "code": "055550", "market": "KOSPI", "reason": "글로벌 금리 연동",         "strength": "보통"},
        {"name": "삼성생명", "code": "032830", "market": "KOSPI", "reason": "금리 수혜 보험사",         "strength": "보통"},
    ],
    "에너지": [
        {"name": "한국전력",       "code": "015760", "market": "KOSPI", "reason": "에너지 가격 연동",      "strength": "보통"},
        {"name": "두산에너빌리티", "code": "034020", "market": "KOSPI", "reason": "원전·에너지 기자재",    "strength": "보통"},
        {"name": "한전KPS",        "code": "051600", "market": "KOSPI", "reason": "발전설비 유지보수",     "strength": "낮음"},
    ],
    "로봇/AI": [
        {"name": "레인보우로보틱스", "code": "277810", "market": "KOSDAQ", "reason": "산업용 로봇 국내 1위", "strength": "높음"},
        {"name": "두산로보틱스",     "code": "454910", "market": "KOSPI",  "reason": "협동로봇 성장",          "strength": "높음"},
        {"name": "HD현대",           "code": "267250", "market": "KOSPI",  "reason": "로봇·AI 사업 보유",      "strength": "보통"},
        {"name": "현대로템",         "code": "064350", "market": "KOSPI",  "reason": "로봇·방산 융합",          "strength": "보통"},
    ],
    "자동차": [
        {"name": "현대차",    "code": "005380", "market": "KOSPI", "reason": "글로벌 자동차 동반 상승",   "strength": "높음"},
        {"name": "기아",      "code": "000270", "market": "KOSPI", "reason": "글로벌 자동차 동반 상승",   "strength": "높음"},
        {"name": "현대모비스", "code": "012330", "market": "KOSPI", "reason": "자동차 부품 공급망",       "strength": "보통"},
        {"name": "만도",      "code": "204320", "market": "KOSPI", "reason": "자동차 부품 공급망",        "strength": "보통"},
    ],
    "소프트웨어": [
        {"name": "NAVER", "code": "035420", "market": "KOSPI", "reason": "AI·플랫폼 섹터 동반",        "strength": "낮음"},
        {"name": "카카오", "code": "035720", "market": "KOSPI", "reason": "AI·플랫폼 섹터 동반",        "strength": "낮음"},
    ],
}

_SYSTEM = """당신은 미국 증시 → 한국 공급망 연동 구조 분석 전문가입니다.

핵심 임무: 전일 미국 섹터 ETF 등락이 한국 어느 섹터·공급망 종목에 구조적으로 연결되는지 분석합니다.
단기 매수 타이밍·"오늘 살 종목" 제시 금지. 공급망 연결 구조와 방향성만 분석합니다.

분석 항목:
1. [미국 강세 섹터 → 한국 공급망 연동] 구조적으로 연결된 한국 섹터·대표 종목 (연동 강도: 높음/보통 구분)
   예: "SMH(반도체ETF) +3.2% → HBM 공급망: SK하이닉스·한미반도체 (연동 높음)"
2. [미국 약세 섹터 → 한국 영향] 하방 압력 받을 수 있는 국내 섹터 (투기적 공매도 신호 아님)
3. [미국 52주 신고가 종목의 공급망 파급] 해당 종목의 한국 부품·소재 공급사 (있는 경우)

출력: "[미국발 한국 섹터 구조적 연동 분석]" 헤더로 시작
- 강세 섹터별 한국 공급망 종목 (연동 강도·근거 포함)
- 주의 필요 섹터 (약세 연동 — 리스크 관리 참고용)
이모지 활용, 간결하게. "지금 매수" "즉시 진입" 표현 절대 사용 금지"""


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
