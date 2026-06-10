"""
strategy_agent.py
주간 종합 투자전략 에이전트 (매주 수요일 20:00 KST 권장)

역할:
  - 단기(이번주) / 중기(1~3개월) / 장기(6개월+) 통합 전략 수립
  - 현재 포트폴리오 리밸런싱 권고
  - 섹터 로테이션 전략
  - 신규 투자처 발굴 (국내 + 미국)
  - 거래량/수급 기반 이슈종목 분석
  - 주간 시장 상황 기반 목표주가 업데이트
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from clients.openai_client import chat
from clients.kis_client import KISClient
from clients.market_data_client import fetch_global_market_data
from clients.telegram_client import send_message, send_error_alert
from clients.us_stock_client import fetch_us_top_movers
from clients.us_market_client import fetch_us_sectors
from services.portfolio_service import calculate_pnl, get_portfolio_summary, get_portfolio_history
from services.watchlist_service import get_watchlist
from services.recommendation_service import get_recent_recommendations, get_performance_stats
from agents.midterm_agent import KOSPI_TOP30

logger = logging.getLogger(__name__)
_TZ = ZoneInfo("Asia/Seoul")

_SYSTEM = """당신은 20년 경력의 포트폴리오 전략가입니다.
주간 시장 데이터와 투자자의 실제 포트폴리오를 바탕으로 중장기 종합 투자전략을 수립하세요.
단기 매매 타이밍 제시 금지. 구조적 섹터 방향과 포트폴리오 배분 전략에 집중합니다.

[전략 수립 철학]
- 이번 주 시장 환경: 매크로·수급 흐름으로 섹터 로테이션 방향 판단
- 중기(1~3개월): 펀더멘털·섹터 로테이션 중심, 아직 미반영된 이익 성장주 발굴
- 장기(6개월+): 가치 투자·배당 성장, 경기 사이클 반영 자산 배분
- 세 관점이 충돌할 때: 장기 방향성 우선

[투자처 발굴 기준]
1. 거래대금 급증 + 외국인·기관 동시 순매수 → 구조적 수급 주도주 발굴
2. 미국 시장 섹터 강세 → 국내 공급망 수혜 미반영 종목 매핑
3. 선물·매크로 이슈 → 관련 현물 섹터 영향 분석
4. 52주 신고가 돌파 종목 → 업황 사이클 방향 확인
5. 외국인/기관 연속 순매수 → 중장기 수급 주도주 발굴

[목표주가 산출 방식]
- 중기: EPS × PER 밴드 상단 (3~6개월)
- 장기: DCF·PBR 적정 밴드 상단 (12개월)

[출력 규칙]
- 확률 수치 필수, 구체적 수치 기반
- "OO가 좋아 보인다" 금지 → "OO 상승확률 70% — 근거: EPS +15%, 외국인 3일 연속 순매수"
- 한국어 텔레그램 텍스트"""

# 주요 관심 미국 섹터 리더 종목
_US_SECTOR_LEADERS = [
    ("NVDA", "엔비디아", "AI반도체"),
    ("SMCI", "슈퍼마이크로", "AI서버"),
    ("AMD", "AMD", "반도체"),
    ("TSMC", "TSMC", "파운드리"),
    ("META", "메타", "소셜AI"),
    ("AMZN", "아마존", "클라우드"),
    ("MSFT", "마이크로소프트", "클라우드AI"),
    ("GOOGL", "알파벳", "AI검색"),
    ("LLY", "일라이릴리", "비만치료제"),
    ("IONQ", "IonQ", "양자컴퓨팅"),
    ("PLTR", "팔란티어", "데이터AI"),
    ("ASTS", "AST스페이스모바일", "위성통신"),
]


def _fetch_us_leaders_data() -> str:
    """미국 섹터 리더 종목 데이터 수집."""
    try:
        import yfinance as yf
        lines = []
        for ticker, name, sector in _US_SECTOR_LEADERS:
            try:
                t = yf.Ticker(ticker)
                hist = t.history(period="5d")
                if hist.empty:
                    continue
                latest = hist.iloc[-1]
                prev   = hist.iloc[-2] if len(hist) > 1 else hist.iloc[-1]
                chg_pct = round((latest["Close"] - prev["Close"]) / prev["Close"] * 100, 2)
                week_chg = round((latest["Close"] - hist.iloc[0]["Close"]) / hist.iloc[0]["Close"] * 100, 2)
                vol_ratio = round(latest["Volume"] / hist["Volume"].mean(), 2) if hist["Volume"].mean() else 1
                lines.append(
                    f"{name}({ticker}) [{sector}]: ${latest['Close']:.2f} "
                    f"(1일 {chg_pct:+.2f}% | 주간 {week_chg:+.2f}% | 거래량비율 {vol_ratio:.1f}x)"
                )
            except Exception:
                pass
        return "\n".join(lines) if lines else "미국 데이터 조회 실패"
    except ImportError:
        return "yfinance 없음"


def _fetch_kr_volume_surge(kis: KISClient) -> str:
    """국내 거래량/거래대금 급증 종목 분석."""
    try:
        lines = []
        # KOSPI/KOSDAQ 거래량 상위
        for market, code in [("KOSPI", "J"), ("KOSDAQ", "Q")]:
            try:
                vol_rank = kis.get_volume_rank(code)[:5]
                amt_rank = kis.get_amount_rank(code)[:5]
                if vol_rank:
                    lines.append(f"\n{market} 거래량 상위:")
                    for s in vol_rank:
                        name = s.get("hts_kor_isnm", "")
                        chg  = float(s.get("prdy_ctrt", 0))
                        lines.append(f"  {name}: {chg:+.2f}%")
                if amt_rank:
                    lines.append(f"{market} 거래대금 상위:")
                    for s in amt_rank:
                        name = s.get("hts_kor_isnm", "")
                        chg  = float(s.get("prdy_ctrt", 0))
                        lines.append(f"  {name}: {chg:+.2f}%")
            except Exception:
                pass
        return "\n".join(lines) if lines else "거래량 데이터 없음"
    except Exception as e:
        return f"거래량 조회 실패: {e}"


_CEO_SUMMARY_SYSTEM = """아래 주간 종합 투자전략 리포트를 읽고,
일일 브리핑 CEO가 오늘의 단기 판단에 앞서 반드시 알아야 할 핵심만 500자 이내로 압축하라.

[포함 필수 항목]
① 이번 주 매크로 방향 (RISK-ON/OFF + 핵심 근거 한 줄)
② 이번 주 주도/유망 섹터 2~3개 + 반대로 피해야 할 섹터 1개
③ 중기(1~3개월) 핵심 후보 종목 2~3개 (이름·코드·근거 한 줄)
④ 이번 주 절대 하면 안 되는 것 1가지

[출력 형식]
📅 주간전략 요약 (YYYY-MM-DD 기준)
🌐 매크로: [방향] — [근거 한 줄]
📈 주도: [섹터] | ❌ 회피: [섹터]
🎯 중기후보: 종목명(코드) — 근거 / 종목명(코드) — 근거
🚫 이번 주 금지: [행동] — [이유]"""


def _generate_ceo_summary(report: str, now: datetime) -> str:
    """주간 전략 전문에서 CEO 일일 브리핑 주입용 500자 요약 생성."""
    try:
        summary = chat(_CEO_SUMMARY_SYSTEM, report[:3000], max_tokens=400)
        return summary
    except Exception as e:
        logger.warning("[전략에이전트] CEO 요약 생성 실패: %s", e)
        # 실패 시 전체 리포트 앞부분 잘라서 반환
        return f"📅 주간전략 ({now.strftime('%Y-%m-%d')})\n{report[:400]}"


def run_strategy():
    """주간 종합 투자전략 실행 및 텔레그램 발송."""
    now = datetime.now(_TZ)
    logger.info("[전략에이전트] 주간 전략 수립 시작: %s", now.strftime("%Y-%m-%d %H:%M"))

    # ── 데이터 수집 ───────────────────────────────────────────

    # 1. 글로벌 시장 데이터
    try:
        market_data = fetch_global_market_data()
    except Exception as e:
        market_data = {}
        logger.warning("[전략에이전트] 글로벌 데이터 실패: %s", e)

    # 2. 미국 섹터 ETF
    try:
        us_sectors = fetch_us_sectors()
    except Exception:
        us_sectors = {}

    # 3. 미국 이슈 종목
    try:
        us_movers = fetch_us_top_movers(n=10)
    except Exception:
        us_movers = []

    # 4. 미국 섹터 리더 데이터
    us_leaders_text = _fetch_us_leaders_data()

    # 5. KIS 국내 수급 데이터
    try:
        kis = KISClient()
        kr_volume_text = _fetch_kr_volume_surge(kis)
        foreign_kospi  = kis.get_foreign_buy_rank("J")[:10]
        foreign_kosdaq = kis.get_foreign_buy_rank("Q")[:10]
        inst_kospi     = kis.get_institution_buy_rank("J")[:10]
    except Exception as e:
        kis = None
        kr_volume_text = "KIS 데이터 없음"
        foreign_kospi = foreign_kosdaq = inst_kospi = []
        logger.warning("[전략에이전트] KIS 실패: %s", e)

    # 6. 보유 포트폴리오
    try:
        portfolio = calculate_pnl(kis)
        summary   = get_portfolio_summary(portfolio)
        history   = get_portfolio_history(days=30)
    except Exception:
        portfolio = []
        summary   = {}
        history   = []

    # 7. 워치리스트
    watchlist = get_watchlist("active")

    # 8. 최근 추천 성과
    try:
        perf = get_performance_stats(days=30)
        recent_recs = get_recent_recommendations(days=14)
    except Exception:
        perf = {}
        recent_recs = []

    # ── 컨텍스트 구성 ─────────────────────────────────────────

    # 글로벌 매크로 요약
    def _mkt(k):
        d = market_data.get(k, {})
        if not d:
            return "N/A"
        return f"{d.get('close', 'N/A')} ({d.get('change_pct', 0):+.2f}%)"

    macro_text = (
        f"S&P500: {_mkt('sp500')} | NASDAQ: {_mkt('nasdaq')} | SOX: {_mkt('sox')}\n"
        f"KOSPI: {_mkt('kospi')} | 원/달러: {_mkt('usdkrw')} | 미국10년금리: {_mkt('us10y')}\n"
        f"VIX: {_mkt('vix')} | 금: {_mkt('gold')} | WTI: {_mkt('wti')}"
    )

    # 미국 섹터 ETF
    sector_lines = []
    for k, v in sorted(us_sectors.items(), key=lambda x: x[1].get("change_pct", 0), reverse=True)[:8]:
        sector_lines.append(f"  {k}: {v.get('change_pct', 0):+.2f}%")
    sectors_text = "\n".join(sector_lines) or "없음"

    # 미국 이슈 종목 (거래량 상위)
    us_movers_text = "\n".join(
        f"  {m.get('name', m.get('ticker', ''))}: {m.get('change_pct', 0):+.2f}% "
        f"(거래량 {m.get('volume_ratio', 1):.1f}x)"
        for m in us_movers[:10]
    ) or "없음"

    # 외국인 수급 요약
    def _fmt_foreign(stocks):
        return "\n".join(
            f"  {s.get('hts_kor_isnm', '')}: {float(s.get('prdy_ctrt', 0)):+.2f}%"
            for s in stocks[:5]
        ) or "없음"

    foreign_text = f"KOSPI:\n{_fmt_foreign(foreign_kospi)}\nKOSDAQ:\n{_fmt_foreign(foreign_kosdaq)}"

    # 포트폴리오 현황
    portfolio_text = "보유 없음"
    if portfolio:
        tf_map = {"short": "단기", "mid": "중기", "long": "장기"}
        p_lines = [
            f"  {p['name']}({p['code']}) [{tf_map.get(p['timeframe'], '단기')}]: "
            f"{p['pnl_pct']:+.2f}% | {p['status_flag']}"
            + (f" | 목표가 {p['target_price']:,.0f}원" if p.get("target_price") else "")
            for p in portfolio
        ]
        total = summary.get("total_pnl_pct", 0)
        portfolio_text = f"총 손익: {total:+.2f}%\n" + "\n".join(p_lines)

    # 최근 추천 성과
    perf_text = (
        f"최근 30일: 총 {perf.get('total', 0)}건 | 승률 {perf.get('win_rate', 0)}% "
        f"| 평균수익률 {perf.get('avg_return', 0):+.2f}% | 손익비 {perf.get('profit_factor', 0):.2f}"
        if perf.get("total", 0) >= 3 else "추천 이력 부족"
    )

    # 워치리스트
    watchlist_text = "\n".join(
        f"  {w['name']}({w['code']}) [{w.get('timeframe', 'short')}]: {w.get('reason', '미기재')}"
        for w in watchlist[:10]
    ) or "없음"

    context = f"""분석 기준일: {now.strftime('%Y년 %m월 %d일 (%A)')}

[글로벌 매크로]
{macro_text}

[미국 섹터 ETF 주간 등락]
{sectors_text}

[미국 이슈 종목 (거래량 상위)]
{us_movers_text}

[미국 섹터 리더 (주간 성과)]
{us_leaders_text}

[국내 거래량/거래대금 상위]
{kr_volume_text}

[외국인 순매수 상위]
{foreign_text}

[현재 포트폴리오]
{portfolio_text}

[관심종목 워치리스트]
{watchlist_text}

[최근 추천 성과]
{perf_text}"""

    prompt = f"""{_SYSTEM}

━━━━━━━━━━━━━━━━━━━━━━━━━━
🗓 주간 종합 투자전략 ({now.strftime('%Y.%m.%d')})
━━━━━━━━━━━━━━━━━━━━━━━━━━

⚡ 이번 주 핵심 결론  (한 줄)
→ [공격 / 선별 / 방어 / 관망]  |  핵심 근거 한 줄

① 시장 환경 + 섹터 포지셔닝
시장환경: [위험 선호 / 안전 선호 / 혼재]  근거: [수치 한 줄]
주도 섹터: [섹터]  |  회피 섹터: [섹터]
미국→국내 수혜: 종목명(코드) — [이유 한 줄]  상승확률: XX%

② 국내 수급 이슈 종목 (핵심 3개)
[거래량·외국인·기관 수급 분석 결과]
종목명(코드) — 수급 특징  |  목표: XX원  |  상승확률: XX%
  매수 조건: [진입 조건]  손절 조건: [이탈 기준]

③ 포트폴리오 리밸런싱
현재: [단기X% / 중기Y%]
  ➕ 확대: [종목/섹터] — 이유
  ➖ 축소: [종목/섹터] — 이유
  🔄 교체: [기존 → 신규] — 이유 (해당 없으면 생략)

④ 단기 + 중기 핵심 후보
[단기 — 이번 주 1~2주]
  종목명(코드)  진입: XX원  손절: YY원  목표: ZZ원  비중: X%  손익비 Z:1
[중기 — 1~3개월]
  종목명(코드)  목표: XX원 (+YY% 현재 대비)  근거: [팩트 한 줄]

⑤ 이번 주 절대 금지 + 이벤트 캘린더
❌ [금지 행동] — 이유: [수치 근거]
📅 [날짜] [이벤트] → 예상 영향: [섹터/종목]
━━━━━━━━━━━━━━━━━━━━━━━━━━"""

    try:
        report = chat(prompt, context, max_tokens=4000)
    except Exception as e:
        logger.error("[전략에이전트] OpenAI 실패: %s", e)
        send_error_alert(f"주간 전략 OpenAI 오류: {e}")
        return

    header = (
        f"🗓 *AI 주간 종합 투자전략* ({now.strftime('%Y.%m.%d')})\n"
        f"단기·중기·장기 통합 | 포트폴리오 리밸런싱 | 이슈종목 발굴\n\n"
    )
    send_message(header + report)

    # ── CEO 일일 브리핑 주입용 압축 요약 생성 + DB 저장 ────────────────
    try:
        ceo_summary = _generate_ceo_summary(report, now)
        from services.strategy_service import save_strategy_report
        save_strategy_report(
            date=now.strftime("%Y-%m-%d"),
            report=report,
            ceo_summary=ceo_summary,
            report_type="weekly",
        )
        logger.info("[전략에이전트] DB 저장 완료")
    except Exception as e:
        logger.warning("[전략에이전트] DB 저장 실패: %s", e)

    logger.info("[전략에이전트] 완료")


def run(state: dict = None) -> dict:
    run_strategy()
    return state or {}
