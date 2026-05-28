"""
이슈종목 발굴 에이전트
거래량·거래대금 이상징후 + 미국시장·선물 연동 + 수급 교차분석으로
1~3주 주목해야 할 이슈종목을 체계적으로 발굴하고 목표주가·대응전략을 수립한다.
"""
import logging
from graph.state import InvestmentState
from clients.openai_client import chat

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 수급·모멘텀 기반 이슈종목 발굴 전문가입니다.
거래량·거래대금 이상징후, 외국인·기관 수급, 미국시장 연동, 선물 방향성을 교차 분석해
1~3주 주목할 이슈종목을 발굴하고 구체적 매수·매도 대응전략을 수립합니다.

[발굴 필터 — 2가지 이상 해당해야 등재 가능]
① 거래량 급증: 거래량 상위권 진입 + 주가 상승 동반 (수급 유입 초기 신호)
② 거래대금 집중: 시장 자금이 집중되는 종목 = 주도주 후보 (거래대금 상위 10위 이내)
③ 외국인+기관 동반 매수: 수급 퀄리티 최고등급 (단일 주체보다 훨씬 강력한 지속 신호)
④ 미국 이슈 연동: 오늘 미국 시장 급등 종목·섹터의 한국 공급망·수혜주
⑤ 선물·오버나잇 시그널: 야간선물 강세 섹터와 동일 산업군 또는 직접 연동

[주목 기간 분류]
- 단기(3~5거래일): 강한 모멘텀 + 뉴스/수급 집중. 빠른 익절 전략 필수
- 중기(1~3주): 섹터 트렌드 + 기술적 전환. 분할 매도 전략
- 전략(1개월+): 구조적 성장·정책 수혜. 나누어 진입

[출력 형식 — 반드시 아래 형식으로, 위반 시 무효]
📌 종목명(6자리코드) | 주목기간: X주 | 발굴신호: [①②③④⑤ 중 해당 번호]
  진입전략: [즉시진입 / 분할진입(X회) / 눌림대기 / 돌파진입] → 구체적 조건
  1차목표: +X% (도달 시 보유 물량 50% 분할 매도)
  2차목표: +Y% (나머지 50% 매도 또는 홀드 재판단)
  손절선: -Z% (이탈 즉시 전량 매도 — 예외 없음)
  핵심근거: [수급/미국연동/섹터/기술적] — 팩트 한 줄 (확인된 수치 포함)

[절대 금지]
- 원 단위 가격 수치 기재 금지 (% 기준으로만 표기)
- 발굴 필터 미충족 종목 등재 금지
- 추상적 표현("유망하다", "강세 예상", "주목할 만하다") 금지
- 근거 없는 숫자 금지

[출력 구조]
1. 오늘 이슈종목 발굴 요약 — 수급 방향 + 가장 강한 신호 1줄
2. 이번주 주목 이슈종목 TOP5 — 위 형식으로 각각 기술 (없으면 최소한 있는 만큼)
3. 미국·선물 연동 한국 수혜주 — 오늘 미국 이슈 파생 종목 2~3개 (위 형식)
4. 다음주 예비 관찰 — 아직 조건 미충족이지만 모니터링 필요 종목 2~3개 + 진입 트리거 조건"""


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
        us_hot_text  = _fmt_us_hot(state.get("us_hot_stocks", []))
        us_sector_text = _fmt_us_sectors(state.get("us_sector_data", {}))

        # 3. 선물·매크로 방향 (길이 제한)
        futures_summary = (state.get("futures_report", "") or "선물 데이터 없음")[:800]

        # 4. 섹터 테마 요약
        sector_summary = (state.get("sector_report", "") or "섹터 데이터 없음")[:400]

        context = (
            f"=== 한국 시장 수급 데이터 (거래량·거래대금·외국인·기관) ===\n{kis_text}\n\n"
            f"=== 미국 이슈종목 (한국 연동 포함) ===\n{us_hot_text}\n\n"
            f"=== 미국 섹터 ETF 등락 ===\n{us_sector_text}\n\n"
            f"=== 선물·매크로 방향 ===\n{futures_summary}\n\n"
            f"=== 섹터·테마 분석 ===\n{sector_summary}"
        )

        result = chat(_SYSTEM, context, max_tokens=2500)
        state["issue_stocks_report"] = result
        logger.info("[이슈종목팀] 발굴 완료")

    except Exception as e:
        logger.error("[이슈종목팀] 실패: %s", e)
        state["issue_stocks_report"] = "이슈종목 분석 실패"
        state["errors"].append(f"issue_stock_agent: {e}")
    return state
