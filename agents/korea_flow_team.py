"""
agents/korea_flow_team.py
수급·현물·섹터 통합 분석 에이전트

korea_spot_market_team + sector_theme_team + money_flow_team 통합
- LLM 호출: 3회 → 2회 (한국현물+섹터 통합 / 수급 분리)
- KIS 데이터 1회 파싱으로 3개 리포트 + candidates + sector_scores 생성
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from graph.state import InvestmentState
from clients.openai_client import chat
from services.scoring_service import score_stock
from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

# ── 섹터 매핑 (sector_theme_team에서 이관) ─────────────────────────────────
_STOCK_SECTOR: dict[str, str] = {
    "삼성전자": "반도체", "SK하이닉스": "반도체", "한미반도체": "반도체",
    "리노공업": "반도체", "원익IPS": "반도체", "HPSP": "반도체",
    "이오테크닉스": "반도체", "케이씨텍": "반도체", "DB하이텍": "반도체",
    "피에스케이": "반도체", "동진쎄미켐": "반도체", "솔브레인": "반도체",
    "카카오": "IT", "NAVER": "IT", "크래프톤": "IT",
    "넷마블": "IT", "엔씨소프트": "IT", "카카오페이": "IT", "카카오뱅크": "IT",
    "현대차": "자동차", "기아": "자동차", "현대모비스": "자동차",
    "현대위아": "자동차", "만도": "자동차", "HL만도": "자동차",
    "한온시스템": "자동차", "현대글로비스": "자동차", "기아차": "자동차",
    "레인보우로보틱스": "로봇", "두산로보틱스": "로봇", "현대로템": "로봇",
    "로보티즈": "로봇", "에스피지": "로봇", "HD현대": "로봇", "티로보틱스": "로봇",
    "LG에너지솔루션": "2차전지", "삼성SDI": "2차전지", "에코프로비엠": "2차전지",
    "포스코퓨처엠": "2차전지", "엘앤에프": "2차전지", "에코프로": "2차전지",
    "SK이노베이션": "2차전지", "일진머티리얼즈": "2차전지",
    "삼성바이오로직스": "바이오", "셀트리온": "바이오", "유한양행": "바이오",
    "한미약품": "바이오", "종근당": "바이오", "녹십자": "바이오",
    "오스코텍": "바이오", "HLB": "바이오",
    "한화에어로스페이스": "방산", "LIG넥스원": "방산", "한국항공우주": "방산",
    "빅텍": "방산", "퍼스텍": "방산",
    "KB금융": "금융", "신한지주": "금융", "하나금융지주": "금융",
    "우리금융지주": "금융", "기업은행": "금융", "삼성생명": "금융",
    "한국금융지주": "금융", "메리츠금융지주": "금융",
    "한국전력": "에너지", "두산에너빌리티": "에너지", "한전KPS": "에너지", "한전기술": "에너지",
    "현대건설": "건설", "GS건설": "건설", "대우건설": "건설",
    "DL이앤씨": "건설", "HDC현대산업개발": "건설",
    "LG생활건강": "소비재", "아모레퍼시픽": "소비재", "CJ제일제당": "소비재",
    "오리온": "소비재", "하이트진로": "소비재",
    "SK텔레콤": "통신", "KT": "통신", "LG유플러스": "통신",
    "HD현대중공업": "조선", "삼성중공업": "조선", "한화오션": "조선", "현대미포조선": "조선",
    "포스코홀딩스": "철강", "현대제철": "철강", "LG화학": "화학",
    "롯데케미칼": "화학", "금호석유": "화학",
}

_ALL_SECTORS = [
    "반도체", "IT", "자동차", "로봇", "2차전지", "바이오", "방산",
    "금융", "에너지", "건설", "소비재", "통신", "조선", "철강", "화학",
]

_RANK_KEYS = [
    ("kospi_amount_rank",       "KOSPI 거래대금"),
    ("kosdaq_amount_rank",      "KOSDAQ 거래대금"),
    ("kospi_rise_rank",         "KOSPI 급등"),
    ("kosdaq_rise_rank",        "KOSDAQ 급등"),
    ("kospi_foreign_rank",      "KOSPI 외국인 순매수"),
    ("kosdaq_foreign_rank",     "KOSDAQ 외국인 순매수"),
    ("kospi_institution_rank",  "KOSPI 기관 순매수"),
    ("kosdaq_institution_rank", "KOSDAQ 기관 순매수"),
    ("kospi_volume_rank",       "KOSPI 거래량"),
    ("kosdaq_volume_rank",      "KOSDAQ 거래량"),
]

# ── LLM 시스템 프롬프트 ───────────────────────────────────────────────────────

_SYSTEM_SPOT_SECTOR = """당신은 한국 주식시장 수급·현물·섹터 통합 분석 전문가입니다.
KIS 거래량·거래대금·수급 데이터로 두 가지 분석을 출력하세요.

[출력 형식 — 반드시 아래 구분자 사용]
=== 한국 현물 분석 ===
1. 오늘 실제 주도 섹터·테마 (거래대금 기준)
2. 외국인·기관 동시 매수 종목 (수급 최상)
3. 급등 상위 종목 재료·지속성 판단
4. 미국-한국 디커플링 여부
5. 오늘 실질 주도 종목 TOP5 (거래대금·수급·등락률 교차)

=== 섹터 순환매 분석 ===
1. [실제 주도 섹터 TOP3] 거래대금 점수 + 수급 근거
2. [US 연동 검증] 미국발 예측과 실제 국내 수급 일치 여부 — 불일치 시 원인
3. [순환매 신호] 자금이 어디서 어디로 이동 중인가
4. [약세·회피 섹터]
5. [핵심 투자 테마 2~3개]"""

_SYSTEM_MONEY_FLOW = """당신은 수급 분석 전문가입니다.
후보 종목 수급 데이터와 EWY/EEM 글로벌 자금 흐름을 분석하세요.

분석 항목:
1. 거래량 급증 + 주가 상승 종목 (수급 유입 신호)
2. 외국인 3거래일 연속 순매수 종목 ★ (강력 신호) — 반드시 별도 강조
3. 기관 순매수 종목 (안정적 수급)
4. [글로벌 자금 선행 지표] EWY·EEM 분석
   - EWY(한국ETF) 상승 → 내일 외국인 순매수 기대
   - EWY vs EEM 비교: EWY > EEM → 한국 단독 강세 알파

출력:
- [글로벌 자금 선행 신호] EWY·EEM → 내일 외국인 수급 방향 예측
- 수급 집중 종목 TOP5 (이유 + 강도 상/중/하)
- 외국인 3거래일 연속 순매수 종목 ★ (없으면 "해당 없음" 명시)
- 전체 수급 판단 (매수우위·매도우위·중립)"""


# ── 결정적 함수들 (LLM 없음) ─────────────────────────────────────────────────

def _calc_sector_scores(raw_kis: dict) -> dict[str, dict]:
    sector_score: dict[str, int] = {}
    sector_stocks: dict[str, list[str]] = {}
    for key, weight in [
        ("kospi_amount_rank", 2), ("kosdaq_amount_rank", 2),
        ("kospi_volume_rank", 1), ("kosdaq_volume_rank", 1),
    ]:
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


def _extract_candidates(data: dict, us_sector_data: dict | None = None) -> list[dict]:
    seen: dict[str, dict] = {}
    scores: dict[str, float] = {}
    rank_specs = [
        ("kospi_foreign_rank",      "KOSPI",  3.0, "mksc_shrn_iscd"),
        ("kosdaq_foreign_rank",     "KOSDAQ", 3.0, "mksc_shrn_iscd"),
        ("kospi_institution_rank",  "KOSPI",  2.0, "mksc_shrn_iscd"),
        ("kosdaq_institution_rank", "KOSDAQ", 2.0, "mksc_shrn_iscd"),
        ("kospi_amount_rank",       "KOSPI",  2.0, "stck_shrn_iscd"),
        ("kosdaq_amount_rank",      "KOSDAQ", 2.0, "stck_shrn_iscd"),
        ("kospi_rise_rank",         "KOSPI",  1.5, "stck_shrn_iscd"),
        ("kosdaq_rise_rank",        "KOSDAQ", 1.5, "stck_shrn_iscd"),
        ("kospi_volume_rank",       "KOSPI",  1.0, "stck_shrn_iscd"),
        ("kosdaq_volume_rank",      "KOSDAQ", 1.0, "stck_shrn_iscd"),
    ]
    for key, market, weight, code_field in rank_specs:
        for rank_i, item in enumerate(data.get(key, [])[:15]):
            code = item.get(code_field) or item.get("stck_shrn_iscd", "")
            if not code:
                continue
            name = item.get("hts_kor_isnm", "")
            chg  = float(item.get("prdy_ctrt", 0) or 0)
            pts  = (15 - rank_i) * weight
            scores[code] = scores.get(code, 0) + pts
            if code not in seen:
                seen[code] = {"code": code, "name": name, "change_pct": chg,
                              "market": market, "score": 0, "source": "KIS"}
            elif name:
                seen[code]["name"] = name
    for code, s in seen.items():
        s["score"] = round(scores.get(code, 0))
    result = sorted(seen.values(), key=lambda x: x["score"], reverse=True)

    if len(result) < 5 and us_sector_data:
        try:
            from agents.us_impact_agent import US_SECTOR_TO_KR
            existing_codes = {s["code"] for s in result}
            for sector, info in sorted(us_sector_data.items(),
                                       key=lambda x: x[1].get("change_pct", 0), reverse=True)[:4]:
                if info.get("change_pct", 0) <= 0.3:
                    continue
                for s in US_SECTOR_TO_KR.get(sector, []):
                    if s["strength"] == "높음" and s["code"] not in existing_codes:
                        result.append({"code": s["code"], "name": s["name"], "change_pct": 0.0,
                                       "market": s.get("market", "KOSPI"),
                                       "score": max(15, round(info.get("change_pct", 0) * 5 + 15)),
                                       "source": "US_fallback"})
                        existing_codes.add(s["code"])
        except Exception as e:
            logger.warning("[수급현물섹터팀] 미국 섹터 보완 실패: %s", e)
    return result[:20]


def _last_n_trading_days(n: int) -> list[str]:
    result: list[str] = []
    d = datetime.now(_KST)
    while len(result) < n:
        if d.weekday() < 5:
            result.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return result


def _save_foreign_buy(date: str, stocks: list[dict]) -> int:
    try:
        rows = [
            (date,
             s.get("mksc_shrn_iscd") or s.get("stck_shrn_iscd") or s.get("code", ""),
             s.get("hts_kor_isnm") or s.get("name", ""),
             float(s.get("frgn_ntby_qty") or s.get("amount", 0) or 0))
            for s in stocks
            if s.get("mksc_shrn_iscd") or s.get("stck_shrn_iscd") or s.get("code")
        ]
        valid = [(d, c, n, a) for d, c, n, a in rows if a > 0]
        if valid:
            with get_conn() as conn:
                for d, c, n, a in valid:
                    conn.execute(
                        text("INSERT INTO foreign_buy_history (date, code, name, amount) "
                             "VALUES (:date, :code, :name, :amount) "
                             "ON CONFLICT (date, code) DO UPDATE SET name=EXCLUDED.name, amount=EXCLUDED.amount"),
                        {"date": d, "code": c, "name": n, "amount": a},
                    )
        return len(valid)
    except Exception as e:
        logger.debug("[수급현물섹터팀] 외국인 이력 저장 실패: %s", e)
        return 0


def _get_consecutive_foreign_buyers(days: int = 3) -> dict[str, str]:
    try:
        trading_dates = _last_n_trading_days(days)
        placeholders = ", ".join(f":d{i}" for i in range(len(trading_dates)))
        params = {f"d{i}": d for i, d in enumerate(trading_dates)}
        params["days"] = days
        with get_conn() as conn:
            rows = conn.execute(
                text(f"SELECT code, name, COUNT(DISTINCT date) AS cnt "
                     f"FROM foreign_buy_history "
                     f"WHERE date IN ({placeholders}) AND amount > 0 "
                     f"GROUP BY code HAVING cnt >= :days"),
                params,
            ).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


def _fmt_rank(stocks: list[dict], qty_field: str = "frgn_ntby_qty", top_n: int = 10) -> str:
    lines = []
    for s in stocks[:top_n]:
        name = s.get("hts_kor_isnm") or s.get("name", "")
        code = s.get("mksc_shrn_iscd") or s.get("stck_shrn_iscd") or s.get("code", "")
        chg  = float(s.get("prdy_ctrt") or s.get("change_pct", 0) or 0)
        qty  = int(float(s.get(qty_field) or 0))
        qty_str = f"  순매수 {qty:,}주" if qty else ""
        lines.append(f"  {name}({code}): {chg:+.2f}%{qty_str}")
    return "\n".join(lines) or "없음"


# ── 통합 run() ───────────────────────────────────────────────────────────────

def run(state: InvestmentState) -> InvestmentState:
    try:
        raw_kis = state.get("raw_kis_data", {})
        date    = state.get("date", datetime.now(_KST).strftime("%Y-%m-%d"))

        # ① 결정적 계산 (LLM 없음)
        sector_data = _calc_sector_scores(raw_kis)
        state["sector_scores"] = [
            {"sector": s, "score": d["score"], "stocks": d["stocks"]}
            for s, d in sector_data.items()
        ]
        state["candidates"] = _extract_candidates(raw_kis, state.get("us_sector_data", {}))

        # ② DB 작업: 외국인 수급 이력 저장 + 3거래일 연속 순매수 조회
        foreign_stocks = (raw_kis.get("kospi_foreign_rank", []) +
                          raw_kis.get("kosdaq_foreign_rank", []))
        inst_stocks    = (raw_kis.get("kospi_institution_rank", []) +
                          raw_kis.get("kosdaq_institution_rank", []))
        if foreign_stocks:
            saved = _save_foreign_buy(date, foreign_stocks)
            logger.info("[수급현물섹터팀] 외국인 순매수 %d건 DB 저장", saved)

        consec_map = _get_consecutive_foreign_buyers(3)
        if consec_map:
            foreign_name_map = {
                (s.get("mksc_shrn_iscd") or s.get("stck_shrn_iscd") or ""): s.get("hts_kor_isnm", "")
                for s in foreign_stocks
            }
            consecutive_text = "\n".join(
                f"★ {foreign_name_map.get(code) or db_name or code}({code})"
                for code, db_name in consec_map.items()
            )
        else:
            consecutive_text = f"해당 없음 (조회 기준: {', '.join(_last_n_trading_days(3))})"

        # ③ LLM 호출 1: 한국 현물 + 섹터 순환매 통합 분석
        kis_text_parts = []
        for key, label in _RANK_KEYS:
            items = raw_kis.get(key, [])[:10]
            if items:
                names = ", ".join(
                    f"{x.get('hts_kor_isnm', x.get('stck_shrn_iscd', '?'))}({x.get('prdy_ctrt', '')}%)"
                    for x in items
                )
                kis_text_parts.append(f"[{label}] {names}")

        sector_lines = "\n".join(
            f"  {s}: {d['score']}점 ({', '.join(d['stocks'][:4])})"
            for s, d in list(sector_data.items())[:8]
        ) if sector_data else "데이터 없음"

        rotation_history = ""
        try:
            from services.market_archive_service import get_sector_rotation_history
            rotation_history = get_sector_rotation_history(days=3)
        except Exception:
            pass

        spot_sector_context = (
            f"KIS 실시간 데이터:\n" + ("\n".join(kis_text_parts) or "데이터 없음") +
            f"\n\n[거래대금·거래량 기반 섹터 집계]\n{sector_lines}" +
            f"\n\n[미국발 공급망 연동 분석]\n{state.get('us_impact_report', '')}" +
            f"\n\n[섹터 순환매 이력]\n{rotation_history or '이력 없음'}" +
            f"\n\n[빅피겨 발언]\n{state.get('bigfigure_report', '')}" +
            f"\n\n[뉴스]\n{state.get('news_report', '')}"
        )

        combined_result = chat(_SYSTEM_SPOT_SECTOR, spot_sector_context, max_tokens=3000)

        # 두 섹션 파싱
        if "=== 섹터 순환매 분석 ===" in combined_result:
            parts = combined_result.split("=== 섹터 순환매 분석 ===")
            spot_part   = parts[0].replace("=== 한국 현물 분석 ===", "").strip()
            sector_part = parts[1].strip()
        else:
            spot_part   = combined_result
            sector_part = combined_result

        state["korea_spot_report"] = spot_part
        state["sector_report"]     = sector_part

        # AI가 언급한 섹터 보완 (미매핑 섹터)
        mentioned = {s["sector"] for s in state["sector_scores"]}
        for s in _ALL_SECTORS:
            if s in sector_part and s not in mentioned:
                state["sector_scores"].append({"sector": s, "score": 30, "stocks": []})

        # ④ candidates 점수 재계산 (sector_scores 반영)
        for c in state["candidates"]:
            c["score"] = score_stock(c, state["sector_scores"])
        state["candidates"].sort(key=lambda x: x.get("score", 0), reverse=True)

        # ⑤ LLM 호출 2: 수급 분석 (EWY/EEM + 외국인/기관)
        top10_text = "\n".join(
            f"{c.get('name', c.get('code', ''))}: 등락률 {c.get('change_pct', 0)}%, 점수 {c.get('score', 0)}"
            for c in state["candidates"][:10]
        ) or "후보 없음"

        raw_mkt = state.get("raw_market_data", {})
        ewy = raw_mkt.get("ewy", {})
        eem = raw_mkt.get("eem", {})
        ewy_line = f"EWY(한국ETF): {ewy['close']} ({ewy['change_pct']:+.2f}%)" if ewy else "EWY: 데이터 없음"
        eem_line = f"EEM(신흥국ETF): {eem['close']} ({eem['change_pct']:+.2f}%)" if eem else "EEM: 데이터 없음"
        ewy_vs_eem = ""
        if ewy and eem:
            diff = round(ewy.get("change_pct", 0) - eem.get("change_pct", 0), 2)
            ewy_vs_eem = f"EWY vs EEM 차이: {diff:+.2f}% ({'한국 단독 강세 ★' if diff > 0.3 else '한국 단독 약세 ⚠️' if diff < -0.3 else '신흥국 동반'})"

        money_context = (
            f"[글로벌 자금 선행 지표]\n{ewy_line}\n{eem_line}\n{ewy_vs_eem}\n\n"
            f"후보 종목:\n{top10_text}\n\n"
            f"외국인 순매수 상위:\n{_fmt_rank(foreign_stocks, 'frgn_ntby_qty')}\n\n"
            f"기관 순매수 상위:\n{_fmt_rank(inst_stocks, 'inst_ntby_qty')}\n\n"
            f"외국인 3거래일 연속 순매수:\n{consecutive_text}\n\n"
            f"섹터 분석:\n{sector_part}"
        )
        state["money_flow_report"] = chat(_SYSTEM_MONEY_FLOW, money_context, max_tokens=2000)

        logger.info("[수급현물섹터팀] 완료 — 섹터 %d개, 후보 %d종목, 연속매수 %d종목",
                    len(sector_data), len(state["candidates"]), len(consec_map))
    except Exception as e:
        logger.error("[수급현물섹터팀] 실패: %s", e)
        state["korea_spot_report"] = "분석 실패"
        state["sector_report"]     = "분석 실패"
        state["money_flow_report"] = "분석 실패"
        state["errors"].append(f"korea_flow_team: {e}")
    return state
