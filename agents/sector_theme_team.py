import logging
from graph.state import InvestmentState
from clients.openai_client import chat

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 한국 증시 섹터 순환매 분석 전문가입니다.

핵심 임무: KIS 실거래 데이터(거래대금·수급) 기반으로 오늘 한국 시장의 실제 자금 흐름을 분석합니다.
US→Korea 섹터 매핑은 us_impact_agent가 이미 처리했습니다. 당신은 국내 실거래 데이터로 그것을 검증하세요.

분석 항목:
1. [거래대금·수급 기반 실제 주도 섹터] — KIS 집계 점수: 높을수록 실제 자금이 몰린 섹터
2. [US 연동 분석 검증] 미국발 섹터 연동 예측(us_impact_report)과 실제 국내 수급이 일치하는가
   → 일치: 방향성 강화 ★ / 불일치: 국내 독자 요인 존재 → 원인 분석
3. [섹터 순환매 방향] 거래대금 점수 변화로 자금이 어디서 어디로 이동하는가
4. [뉴스·빅피겨 기반 테마 모멘텀] 현재 섹터 흐름에 영향 주는 재료

출력:
- [실제 주도 섹터 TOP3] 거래대금 점수 + 수급 근거 포함
- [약세·회피 섹터] — 거래대금 낮거나 외국인·기관 이탈
- [핵심 투자 테마 2~3개] — 오늘 시장에서 확인된 구조적 테마
- [순환매 신호] 자금이 지금 어디서 어디로 이동 중인지 (섹터 간 로테이션)
- [US 연동 vs 국내 수급 불일치 시] 원인과 판단 명시"""

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

        # 섹터 순환매 이력 로드 (최근 3일 — 오늘 방향 비교용)
        rotation_history = ""
        try:
            from services.market_archive_service import get_sector_rotation_history
            rotation_history = get_sector_rotation_history(days=3)
        except Exception as _e:
            logger.debug("[섹터팀] 순환매 이력 조회 실패: %s", _e)

        context = (
            f"{sector_summary}\n\n"
            f"[미국발 공급망 연동 분석 (us_impact_agent)]\n{state.get('us_impact_report', '')}\n\n"
            f"[섹터 순환매 이력]\n{rotation_history or '이력 없음'}\n\n"
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
