import logging
from graph.state import InvestmentState
from clients.openai_client import chat

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 섹터 순환매 및 테마 분석 전문가입니다.

핵심 원칙: 미국 섹터 ETF의 전일 등락은 오늘 한국 섹터 순환매의 방향을 선행합니다.
미국 섹터 강도 → 한국 섹터 강도 연동을 분석의 출발점으로 삼으세요.

분석 항목:
1. [미국 섹터 → 한국 섹터 연동] 전일 미국 강세 섹터 → 오늘 한국 동일 섹터 기대 강도 매핑
   (예: SMH +3% → 반도체 강세 / XLE +2% → 에너지·정유 강세)
2. [거래대금 기반 실제 주도 섹터] — KIS 데이터 집계 점수 반영
3. 미국 연동과 국내 수급이 겹치는 섹터 = 오늘 가장 강한 섹터 (★ 표시)
4. 뉴스·빅피겨 발언 기반 테마 모멘텀
5. 순환매 방향: 어제 강세 섹터에서 어디로 이동 중인가

출력:
- [오늘 최강 섹터 TOP3] ★ 미국연동+국내수급 겹치는 섹터 우선 (점수·근거 포함)
- [약세·회피 섹터]
- [핵심 투자 테마 3개]
- [순환매 신호] 자금이 지금 어디서 어디로 이동 중인지"""

# 종목명 → 섹터 매핑 (KIS 순위 데이터에서 섹터 자동 분류용)
_STOCK_SECTOR: dict[str, str] = {
    # 반도체
    "삼성전자": "반도체", "SK하이닉스": "반도체", "한미반도체": "반도체",
    "리노공업": "반도체", "원익IPS": "반도체", "HPSP": "반도체",
    "이오테크닉스": "반도체", "케이씨텍": "반도체", "DB하이텍": "반도체",
    "피에스케이": "반도체", "동진쎄미켐": "반도체", "솔브레인": "반도체",
    # IT/플랫폼
    "카카오": "IT", "NAVER": "IT", "크래프톤": "IT",
    "넷마블": "IT", "엔씨소프트": "IT", "카카오페이": "IT", "카카오뱅크": "IT",
    # 자동차
    "현대차": "자동차", "기아": "자동차", "현대모비스": "자동차",
    "현대위아": "자동차", "만도": "자동차", "HL만도": "자동차",
    "한온시스템": "자동차", "현대글로비스": "자동차", "기아차": "자동차",
    # 로봇
    "레인보우로보틱스": "로봇", "두산로보틱스": "로봇", "현대로템": "로봇",
    "로보티즈": "로봇", "에스피지": "로봇", "HD현대": "로봇",
    "티로보틱스": "로봇",
    # 2차전지
    "LG에너지솔루션": "2차전지", "삼성SDI": "2차전지", "에코프로비엠": "2차전지",
    "포스코퓨처엠": "2차전지", "엘앤에프": "2차전지", "에코프로": "2차전지",
    "SK이노베이션": "2차전지", "일진머티리얼즈": "2차전지",
    # 바이오
    "삼성바이오로직스": "바이오", "셀트리온": "바이오", "유한양행": "바이오",
    "한미약품": "바이오", "종근당": "바이오", "녹십자": "바이오",
    "오스코텍": "바이오", "HLB": "바이오",
    # 방산
    "한화에어로스페이스": "방산", "LIG넥스원": "방산", "한국항공우주": "방산",
    "빅텍": "방산", "퍼스텍": "방산",
    # 금융
    "KB금융": "금융", "신한지주": "금융", "하나금융지주": "금융",
    "우리금융지주": "금융", "기업은행": "금융", "삼성생명": "금융",
    "한국금융지주": "금융", "메리츠금융지주": "금융",
    # 에너지
    "한국전력": "에너지", "두산에너빌리티": "에너지", "한전KPS": "에너지",
    "한전기술": "에너지",
    # 건설
    "현대건설": "건설", "GS건설": "건설", "대우건설": "건설",
    "DL이앤씨": "건설", "HDC현대산업개발": "건설",
    # 소비재
    "LG생활건강": "소비재", "아모레퍼시픽": "소비재", "CJ제일제당": "소비재",
    "오리온": "소비재", "하이트진로": "소비재",
    # 통신
    "SK텔레콤": "통신", "KT": "통신", "LG유플러스": "통신",
    # 조선
    "HD현대중공업": "조선", "삼성중공업": "조선", "한화오션": "조선",
    "현대미포조선": "조선",
    # 철강/소재
    "포스코홀딩스": "철강", "현대제철": "철강", "LG화학": "화학",
    "롯데케미칼": "화학", "금호석유": "화학",
}

_ALL_SECTORS = [
    "반도체", "IT", "자동차", "로봇", "2차전지", "바이오", "방산",
    "금융", "에너지", "건설", "소비재", "통신", "조선", "철강", "화학",
]


def _calc_sector_scores(raw_kis: dict) -> dict[str, dict]:
    """KIS 거래대금·거래량 순위에서 섹터별 점수 집계.
    거래대금 순위에 가중치 2, 거래량 순위에 가중치 1. 상위 종목일수록 높은 점수."""
    sector_score: dict[str, int] = {}
    sector_stocks: dict[str, list[str]] = {}

    rank_sources = [
        ("kospi_amount_rank",  2),
        ("kosdaq_amount_rank", 2),
        ("kospi_volume_rank",  1),
        ("kosdaq_volume_rank", 1),
    ]
    for key, weight in rank_sources:
        for rank_i, item in enumerate(raw_kis.get(key, [])[:20]):
            name = item.get("hts_kor_isnm", "")
            sector = _STOCK_SECTOR.get(name)
            if not sector:
                continue
            score = (21 - rank_i) * weight
            sector_score[sector] = sector_score.get(sector, 0) + score
            sector_stocks.setdefault(sector, [])
            if name not in sector_stocks[sector]:
                sector_stocks[sector].append(name)

    return {
        s: {"score": c, "stocks": sector_stocks.get(s, [])}
        for s, c in sorted(sector_score.items(), key=lambda x: x[1], reverse=True)
    }


def run(state: InvestmentState) -> InvestmentState:
    try:
        raw_kis = state.get("raw_kis_data", {})
        sector_data = _calc_sector_scores(raw_kis)

        # 구조화된 섹터 점수 → state에 저장
        state["sector_scores"] = [
            {"sector": s, "score": d["score"], "stocks": d["stocks"]}
            for s, d in sector_data.items()
        ]

        # 섹터 요약 텍스트 (AI 입력용)
        if sector_data:
            sector_lines = "\n".join(
                f"  {s}: {d['score']}점 ({', '.join(d['stocks'][:4])})"
                for s, d in list(sector_data.items())[:8]
            )
            sector_summary = f"[거래대금·거래량 기반 섹터 집계]\n{sector_lines}"
        else:
            sector_summary = "[거래대금·거래량 기반 섹터 집계] 데이터 없음 (장 마감 또는 API 오류)"

        context = (
            f"{sector_summary}\n\n"
            f"[미국시장]\n{state.get('us_market_report', '')}\n\n"
            f"[미국영향]\n{state.get('us_impact_report', '')}\n\n"
            f"[빅피겨발언]\n{state.get('bigfigure_report', '')}\n\n"
            f"[한국현물]\n{state.get('korea_spot_report', '')}\n\n"
            f"[뉴스]\n{state.get('news_report', '')}"
        )
        result = chat(_SYSTEM, context, max_tokens=2000)
        state["sector_report"] = result

        # AI가 언급한 섹터도 반영 (미매핑 섹터 보완)
        mentioned = {s["sector"] for s in state["sector_scores"]}
        for s in _ALL_SECTORS:
            if s in result and s not in mentioned:
                state["sector_scores"].append({"sector": s, "score": 30, "stocks": []})

        logger.info("[섹터팀] 완료 — 집계 섹터 %d개", len(sector_data))
    except Exception as e:
        logger.error("[섹터팀] 실패: %s", e)
        state["sector_report"] = "분석 실패"
        state["errors"].append(f"sector_team: {e}")
    return state
