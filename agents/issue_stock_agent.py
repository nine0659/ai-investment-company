"""
이슈종목 분석 에이전트
오늘 시장 이슈 종목의 배경·원인을 분석하고,
공급망 연결고리와 중장기 투자 인사이트를 도출한다. (단기 매매 신호 생성 아님)
"""
import logging
from graph.state import InvestmentState
from clients.openai_client import chat

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 시장 이슈 분석 및 중장기 투자 인사이트 전문가입니다.
오늘 시장에서 주목받은 종목·섹터의 배경과 원인을 깊이 분석하고,
이것이 시사하는 시장 구조와 앞으로 탐색해야 할 투자 기회를 발굴합니다.

[분석 철학 — 3가지 원칙]
① "왜 올랐나"보다 "이것이 무엇을 의미하는가"를 먼저 생각한다
② 오늘의 이슈가 다음주·다음달에 어떤 공급망·섹터 연결고리를 만드는지 추론한다
③ 단타·초단타 진입 신호 절대 금지. 시장 구조를 읽어 중장기 투자 인사이트를 도출한다

[원인 분류 — 반드시 판단]
• 실적/가이던스: 어닝 서프라이즈·실적 발표·가이던스 상향/하향
• M&A/지분: 인수합병·최대주주 변경·전략적 제휴
• 정책/규제: 정부 수혜·규제 완화·제재·인허가
• 테마 편승: 섹터 전체 테마 유입 — 개별 재료 없음 (지속성 낮음)
• 미국 연동: 미국 관련주 급등·섹터 강세에 따른 국내 수혜 연동
• 수급 주도: 뉴스 없이 외국인·기관 매수 집중 (세력 가능성 유의)
• 공시/이슈: DART 공시·계약·특허·임상 결과
• 기술적: 지지선 반등·돌파 — 개별 재료 없음

[지속성 판단 — 핵심]
- 단발: 재료 소멸 예상 → 이미 오른 종목은 추격 금물, 다른 기회 탐색
- 트렌드 지속: 섹터 사이클 시작 → 아직 안 오른 2차 수혜 탐색
- 구조적 변화: 업종 패러다임 전환 → 공급망 전체 수혜 발굴

[출력 구조 — 반드시 이 순서로]

━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 오늘 시장 이슈 핵심 요약 (3줄 이내)
오늘 시장에서 가장 중요한 흐름 + 배경 + 핵심 시사점

━━━━━━━━━━━━━━━━━━━━━━━━━━
🔍 주요 이슈 종목 배경 분석 (3~5개)
[종목명(코드)] +X% | 원인: [원인분류]
  배경: 구체적 근거 한 줄 (수치 포함, 없으면 "확인된 재료 없음 — 수급 주도 주의")
  시장 의미: [단발 / 트렌드 시작 / 구조적 변화] — 판단 근거 한 줄
  공급망 시사: 이 종목의 상승이 암시하는 다음 수혜 섹터·종목 (구체적으로)

━━━━━━━━━━━━━━━━━━━━━━━━━━
💡 오늘 이슈에서 도출한 투자 인사이트
  오늘 이슈가 시사하는 섹터 흐름 (무엇이 구조적으로 변하고 있는가)
  가격 미반영 공급망 2차 수혜 후보 (오늘 급등 A → 아직 반응 없는 B·C)
  예: 오늘 SK하이닉스 +4% → HBM 소재 하나마이크론 미반영 확인
  예: 오늘 NVDA +5% → 삼성전기(MLCC)·심텍(PCB) 아직 주목 전
  놓치면 안 되는 역발상 포인트 (시장이 과도 반응하거나 무시한 것)

━━━━━━━━━━━━━━━━━━━━━━━━━━
📡 앞으로 모니터링할 것
  다음 트리거가 될 지표·이벤트 (언제, 무엇을 확인하면 투자 검토 진행)
  섹터·종목 체크리스트 (이 흐름이 지속된다면 다음에 주목할 것)

━━━━━━━━━━━━━━━━━━━━━━━━━━
🧭 선행 수급 탐지
  가격 변화 없는데 외국인·기관 순매수 진입 중인 종목 (조용한 매집 신호)
  거래량 상위에는 없지만 외국인/기관 순매수 상위 → 선제 진입 가능성

[절대 금지]
- 진입가·손절가·목표가 원 단위 수치 제시 금지
- "즉시 매수" "지금 진입" "손절 -X%" 등 단기 매매 신호 제시 금지
- 추상적 표현("유망하다", "강세 예상", "주목할 만하다") 금지
- 근거 없는 확정적 표현 금지"""


_RANK_SPECS: list[tuple[str, str, str, str | None]] = [
    # (kis_key, 레이블, 코드필드, 수량필드)
    ("kospi_amount_rank",       "KOSPI 거래대금 상위",  "stck_shrn_iscd", "acml_tr_pbmn"),
    ("kosdaq_amount_rank",      "KOSDAQ 거래대금 상위", "stck_shrn_iscd", "acml_tr_pbmn"),
    ("kospi_volume_rank",       "KOSPI 거래량 상위",    "stck_shrn_iscd", "acml_vol"),
    ("kosdaq_volume_rank",      "KOSDAQ 거래량 상위",   "stck_shrn_iscd", "acml_vol"),
    ("kospi_foreign_rank",      "KOSPI 외국인 순매수",  "mksc_shrn_iscd", "frgn_ntby_qty"),
    ("kosdaq_foreign_rank",     "KOSDAQ 외국인 순매수", "mksc_shrn_iscd", "frgn_ntby_qty"),
    ("kospi_institution_rank",  "KOSPI 기관 순매수",    "mksc_shrn_iscd", "inst_ntby_qty"),
    ("kosdaq_institution_rank", "KOSDAQ 기관 순매수",   "mksc_shrn_iscd", "inst_ntby_qty"),
    ("kospi_rise_rank",         "KOSPI 급등주",         "stck_shrn_iscd", None),
    ("kosdaq_rise_rank",        "KOSDAQ 급등주",        "stck_shrn_iscd", None),
]


def _fmt_rank(items: list[dict], code_field: str, qty_field: str | None, top_n: int = 15) -> str:
    lines = []
    for i, s in enumerate(items[:top_n], 1):
        name = s.get("hts_kor_isnm") or s.get("name", "?")
        code = s.get(code_field) or s.get("stck_shrn_iscd", "")
        chg  = float(s.get("prdy_ctrt", 0) or 0)
        qty_str = ""
        if qty_field:
            qty = s.get(qty_field)
            if qty:
                try:
                    qty_str = f" [{int(float(qty)):,}]"
                except Exception:
                    pass
        lines.append(f"  {i:2d}. {name}({code}) {chg:+.2f}%{qty_str}")
    return "\n".join(lines) if lines else "없음"


def _fmt_us_hot(us_hot: list[dict]) -> str:
    lines = []
    for s in us_hot[:10]:
        ticker = s.get("ticker", "")
        name   = s.get("name", ticker)
        chg    = s.get("change_pct", 0)
        reason = s.get("reason", "")
        kr_rel = s.get("kr_related", [])
        kr_str = ""
        if kr_rel:
            kr_str = " → KR수혜: " + ", ".join(
                f"{r.get('name', '')}({r.get('code', '')})"
                for r in kr_rel[:3]
            )
        reason_str = f" [{reason}]" if reason else ""
        lines.append(f"  {name}({ticker}) {chg:+.2f}%{reason_str}{kr_str}")
    return "\n".join(lines) if lines else "없음"


def _fmt_us_sectors(us_sector: dict) -> str:
    sorted_sectors = sorted(
        us_sector.items(), key=lambda x: x[1].get("change_pct", 0), reverse=True
    )
    lines = [
        f"  {k}: {v.get('change_pct', 0):+.2f}%"
        for k, v in sorted_sectors[:10]
    ]
    return "\n".join(lines) if lines else "없음"


def _extract_stock_news(stock_names: list[str], raw_news_data: dict) -> str:
    """급등·급락 종목명으로 뉴스 데이터에서 관련 기사 추출."""
    if not raw_news_data or not stock_names:
        return "관련 뉴스 없음"

    result_parts = []
    for name in stock_names[:15]:
        matched = []
        for source, articles in raw_news_data.items():
            for article in (articles or []):
                title = article.get("title", "")
                summary = article.get("summary", "")
                if name in title or name in summary:
                    matched.append(f"    [{source}] {title}")
                if len(matched) >= 2:
                    break
            if len(matched) >= 2:
                break
        if matched:
            result_parts.append(f"  {name}:\n" + "\n".join(matched))

    return "\n".join(result_parts) if result_parts else "매핑된 관련 뉴스 없음 (수급 주도 또는 뉴스 수집 범위 밖)"


def _fmt_dart(dart_items: list[dict]) -> str:
    """DART 공시 포맷 — 급등 원인 파악용."""
    if not dart_items:
        return "없음"
    lines = []
    for d in dart_items[:15]:
        corp = d.get("corp_name") or d.get("name", "")
        title = d.get("report_nm") or d.get("title", "")
        dt = d.get("rcept_dt") or d.get("date", "")
        if corp and title:
            lines.append(f"  [{corp}] {title} ({dt})")
    return "\n".join(lines) if lines else "없음"


def run(state: InvestmentState) -> InvestmentState:
    try:
        raw_kis = state.get("raw_kis_data", {})

        # 1. KIS 수급 데이터 포맷
        kis_parts = []
        for key, label, code_field, qty_field in _RANK_SPECS:
            items = raw_kis.get(key, [])
            if items:
                kis_parts.append(f"[{label}]\n{_fmt_rank(items, code_field, qty_field)}")
        kis_text = "\n\n".join(kis_parts) if kis_parts else "KIS 수급 데이터 없음 (장 마감 또는 API 오류)"

        # 2. 미국 이슈종목 + 섹터
        us_hot_text    = _fmt_us_hot(state.get("us_hot_stocks", []))
        us_sector_text = _fmt_us_sectors(state.get("us_sector_data", {}))

        # 3. 선물·매크로 방향 (길이 제한)
        futures_summary = (state.get("futures_report", "") or "선물 데이터 없음")[:800]

        # 4. 섹터 테마 요약
        sector_summary = (state.get("sector_report", "") or "섹터 데이터 없음")[:400]

        # 5. 급등종목 관련 뉴스 추출 (원인 분석용)
        surge_names: list[str] = []
        for key in ("kospi_rise_rank", "kosdaq_rise_rank"):
            for item in raw_kis.get(key, [])[:10]:
                n = item.get("hts_kor_isnm", "")
                if n and n not in surge_names:
                    surge_names.append(n)
        # 거래대금 상위도 포함 (급등 아니어도 이슈 가능)
        for key in ("kospi_amount_rank", "kosdaq_amount_rank"):
            for item in raw_kis.get(key, [])[:5]:
                n = item.get("hts_kor_isnm", "")
                if n and n not in surge_names:
                    surge_names.append(n)

        raw_news = state.get("raw_news_data", {})
        surge_news_text = _extract_stock_news(surge_names, raw_news)

        # 6. DART 공시 (실적·M&A·계약 등 원인 파악)
        dart_text = _fmt_dart(state.get("dart_disclosures", []))

        context = (
            f"=== 한국 시장 수급 데이터 (거래량·거래대금·외국인·기관) ===\n{kis_text}\n\n"
            f"=== 급등·이슈 종목 관련 뉴스 (원인 분석용) ===\n{surge_news_text}\n\n"
            f"=== 오늘 DART 주요 공시 ===\n{dart_text}\n\n"
            f"=== 미국 이슈종목 (한국 연동 포함) ===\n{us_hot_text}\n\n"
            f"=== 미국 섹터 ETF 등락 ===\n{us_sector_text}\n\n"
            f"=== 선물·매크로 방향 ===\n{futures_summary}\n\n"
            f"=== 섹터·테마 분석 ===\n{sector_summary}"
        )

        result = chat(_SYSTEM, context, max_tokens=3000)
        state["issue_stocks_report"] = result
        logger.info("[이슈종목팀] 발굴 완료 (원인분석 포함, 뉴스매핑 %d종목)", len(surge_names))

    except Exception as e:
        logger.error("[이슈종목팀] 실패: %s", e)
        state["issue_stocks_report"] = "이슈종목 분석 실패"
        state["errors"].append(f"issue_stock_agent: {e}")
    return state
