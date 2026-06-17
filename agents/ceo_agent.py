"""
agents/ceo_agent.py
CIO (Chief Investment Officer) — 최고투자책임자

역할:
- 포트폴리오 최종 의사결정권자 (진입·청산·비중 결정)
- 월간 투자 테제 유지·수정 판단
- 분석팀 인텔리전스 독립 검토
- 텔레그램 브리핑 최종 발행

철학: 버핏(해자·가치) + 멍거(역발상) + 달리오(매크로 사이클) + 손자(손실 방어 우선)
"""
import logging
import re
from datetime import datetime
from graph.state import InvestmentState
from clients.openai_client import chat_ceo
from clients.kis_client import KISClient
from clients.us_stock_client import format_us_impact_for_prompt

from services.recommendation_service import (
    update_close_prices, format_returns_for_report, get_performance_stats,
)
from config.settings import RUN_TYPE_GLOBAL, RUN_TYPE_PRE, RUN_TYPE_INTRA1, RUN_TYPE_INTRA2, RUN_TYPE_CLOSE, TZ

logger = logging.getLogger(__name__)

# ── 블루칩 목록 (컨센서스 수집용) ─────────────────────────────────────────
_BLUECHIP_ALWAYS_FETCH: list[dict] = [
    {"code": "005930", "name": "삼성전자",         "market": "KOSPI"},
    {"code": "000660", "name": "SK하이닉스",       "market": "KOSPI"},
    {"code": "373220", "name": "LG에너지솔루션",   "market": "KOSPI"},
    {"code": "207940", "name": "삼성바이오로직스",  "market": "KOSPI"},
    {"code": "005380", "name": "현대차",            "market": "KOSPI"},
    {"code": "005490", "name": "POSCO홀딩스",      "market": "KOSPI"},
    {"code": "035420", "name": "NAVER",             "market": "KOSPI"},
    {"code": "035720", "name": "카카오",            "market": "KOSPI"},
    {"code": "068270", "name": "셀트리온",          "market": "KOSPI"},
    {"code": "012330", "name": "현대모비스",        "market": "KOSPI"},
]


# ── CIO 결정 로그 파싱 ────────────────────────────────────────────────────
_LOG_RE = re.compile(r"=CIO_DECISION_START=[ \t]*\n(.*?)\n[ \t]*=CIO_DECISION_END=", re.DOTALL)


def _parse_cio_decisions(text: str, date: str, run_type: str) -> tuple[str, dict]:
    """CIO 결정 로그 블록을 파싱 후 (cleaned_text, decisions_dict) 반환.

    로그 블록은 텔레그램 메시지에서 제거된다.
    """
    base: dict = {
        "date": date,
        "run_type": run_type,
        "macro_stance": "neutral",
        "cash_target_pct": 30,
        "thesis_status": "intact",
        "committee_alignment": "agree",
        "committee_dissent": "",
        "new_positions": [],
        "position_changes": [],
        "position_holds": [],
        "key_risks": [],
        "strategic_note": "",
    }

    m = _LOG_RE.search(text)
    if not m:
        return text, base

    block   = m.group(1)
    cleaned = (text[: m.start()] + text[m.end() :]).strip()

    for raw_line in block.split("\n"):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        key   = parts[0].lower() if parts else ""

        try:
            if key == "stance" and len(parts) >= 3:
                base["macro_stance"]     = parts[1]
                base["cash_target_pct"]  = int(parts[2])
            elif key == "thesis" and len(parts) >= 2:
                base["thesis_status"] = parts[1]
            elif key == "analyst" and len(parts) >= 2:
                base["committee_alignment"] = parts[1]
                base["committee_dissent"]   = parts[2] if len(parts) > 2 else ""
            elif key == "new" and len(parts) >= 5:
                base["new_positions"].append({
                    "code":          parts[1],
                    "name":          parts[2],
                    "size_pct":      float(parts[3]),
                    "conviction":    parts[4],
                    "timeframe":     parts[5] if len(parts) > 5 else "mid",
                    "thesis":        parts[6] if len(parts) > 6 else "",
                    "thesis_stage":  parts[7] if len(parts) > 7 else "developing",
                    "risk_reward":   parts[8] if len(parts) > 8 else "",
                    "falsification": parts[9] if len(parts) > 9 else "",
                })
            elif key in ("reduce", "add", "exit") and len(parts) >= 3:
                base["position_changes"].append({
                    "action":           key,
                    "code":             parts[1],
                    "name":             parts[2],
                    "size_change_pct":  float(parts[3]) if len(parts) > 3 else 0,
                    "reason":           parts[4] if len(parts) > 4 else "",
                })
            elif key == "hold" and len(parts) >= 3:
                base["position_holds"].append({
                    "code":            parts[1],
                    "name":            parts[2],
                    "conviction":      parts[3] if len(parts) > 3 else "medium",
                    "thesis_stage":    parts[4] if len(parts) > 4 else "developing",
                    "review_trigger":  parts[5] if len(parts) > 5 else "",
                    "falsification":   parts[6] if len(parts) > 6 else "",
                })
            elif key == "risk" and len(parts) >= 2:
                base["key_risks"].append(parts[1])
            elif key == "note" and len(parts) >= 2:
                base["strategic_note"] = parts[1]
        except Exception:
            pass  # 파싱 실패한 줄은 조용히 무시

    return cleaned, base


# ── 급등종목 수급 교차분석 (CEO 판단용) ──────────────────────────────────────
def _format_surge_context(raw_kis_data: dict, top_n: int = 10) -> str:
    surge_items: list[tuple[str, str, float, str]] = []
    for market_label, rise_key in [("KOSPI", "kospi_rise_rank"), ("KOSDAQ", "kosdaq_rise_rank")]:
        for item in raw_kis_data.get(rise_key, [])[:top_n]:
            code = item.get("stck_shrn_iscd", "")
            name = item.get("hts_kor_isnm", code)
            chg  = float(item.get("prdy_ctrt", 0) or 0)
            if code and chg > 0:
                surge_items.append((code, name, chg, market_label))

    if not surge_items:
        return ""

    foreign_codes = {
        item.get("mksc_shrn_iscd", "")
        for key in ("kospi_foreign_rank", "kosdaq_foreign_rank")
        for item in raw_kis_data.get(key, [])[:20]
        if item.get("mksc_shrn_iscd")
    }
    institution_codes = {
        item.get("mksc_shrn_iscd", "")
        for key in ("kospi_institution_rank", "kosdaq_institution_rank")
        for item in raw_kis_data.get(key, [])[:20]
        if item.get("mksc_shrn_iscd")
    }

    lines = ["[급등종목 수급 교차분석]"]
    for code, name, chg, market in sorted(surge_items, key=lambda x: -x[2]):
        f_buy = code in foreign_codes
        i_buy = code in institution_codes
        if f_buy and i_buy:
            quality = "수급 최상 (외국인+기관)"
        elif f_buy:
            quality = "외국인 순매수"
        elif i_buy:
            quality = "기관 순매수"
        else:
            quality = "수급 미확인"
        lines.append(f"  {name}({code}) [{market}] +{chg:.1f}% | {quality}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
#  CIO 정체성과 의사결정 철학
# ══════════════════════════════════════════════════════════════════

_CIO_CHARTER = """
━━━━━━━━━━━━━━━━━━━━━━━━━━
[CIO 역할과 책임]
━━━━━━━━━━━━━━━━━━━━━━━━━━
당신은 한국 주식 전문 자산운용사의 CIO(최고투자책임자)입니다.

책임 범위:
① 월간 투자 테제(Investment Thesis) 설정·유지·수정
② 모든 포지션 진입·청산·비중 조절 최종 결정
③ 분석팀 인텔리전스 독립 검토 후 투자 의사결정
④ 포트폴리오 전체 리스크 예산 관리

━━━━━━━━━━━━━━━━━━━━━━━━━━
[투자 철학 — 의사결정의 기준]
━━━━━━━━━━━━━━━━━━━━━━━━━━
▶ 달리오(매크로): 매크로 레짐이 먼저다.
  레짐이 불리하면 아무리 좋은 종목도 소규모 접근.
  경기·부채·유동성·달러 사이클이 지금 어느 위치인가.

▶ 버핏(가치·해자): 구조적 해자와 업황 사이클이 있는 기업만.
  "이 기업은 3년 후에도 지금 가격보다 더 가치 있는가."
  밸류에이션이 과거 사이클 대비 어디에 있는가.

▶ 멍거(역발상): 지금 시장이 당연하다고 믿는 것이 틀릴 조건은 무엇인가.
  다수가 한 방향으로 쏠릴수록 반대편의 비대칭 기회를 먼저 확인한다.

▶ 손자(손실 방어): 먼저 지지 않는 조건을 갖춘다(先為不可勝).
  자산 보존이 수익 추구보다 언제나 우선이다.

━━━━━━━━━━━━━━━━━━━━━━━━━━
[포트폴리오 운용 원칙]
━━━━━━━━━━━━━━━━━━━━━━━━━━
- 집중 투자: 확신 있는 5~10개 포지션만 보유
- 확신 상: 포트폴리오 5~10% / 중: 3~5% / 하: 1~3%
- 보유 기간: 신규 진입 시 최소 3개월 이상 보유를 전제로 판단
- 진입: "구간 내 분할 접근" — 특정 날 특정 가격 타이밍 금지
- 손절 기준: 투자 thesis 훼손 기준 (기계적 % 손절이 아닌 논리 기반)
- 절대 금지: 레버리지·미수·신용·추격 매수·단기 노이즈 대응

[포지션 생애주기 — 비중 조절 기준]
EARLY    (초기):  시장 미인식 구간 → 1~3% 탐색 진입
DEVELOPING (성장): 모멘텀 형성·일부 인식 시작 → 3~7% 비중 확대
MATURE   (성숙):  컨센서스 형성·업사이드 축소 → 유지 또는 점진 축소
EXHAUSTED (소진): thesis 현실화 완료 → 청산 검토·신규 기회 탐색

[손익비(R/R) 원칙 — 진입 기준]
- 모든 신규 포지션: 업사이드(%) ÷ 다운사이드(%) ≥ 3:1 이상이어야 진입
- 업사이드: 컨센서스 목표주가(또는 CIO 내재가치) 대비 현재가 상승 여력
- 다운사이드: thesis 훼손 손절가 대비 현재가 하락 폭
- 손익비 3:1 미만이면 신규 진입 보류·현금 유지

━━━━━━━━━━━━━━━━━━━━━━━━━━
[CIO 독립 판단 4원칙 — 필수]
━━━━━━━━━━━━━━━━━━━━━━━━━━
① 분석팀 보고서와 독립적으로 팩트 시트 수치만으로 먼저 CIO 자신의 가설을 정립할 것
② "시장 컨센서스(다수 의견)가 옳다면 나는 무엇을 잃는가" → 반드시 역발상 기회 확인
③ 각 포지션에 "이것이 발생하면 thesis가 훼손된 것"이라는 반증 신호를 반드시 명시할 것
④ 분석팀과 이견이 없을 때도 "시장 컨센서스와 CIO 뷰가 같은/다른 이유"를 한 줄로 명시

━━━━━━━━━━━━━━━━━━━━━━━━━━
[분석팀 독립 검토 원칙]
━━━━━━━━━━━━━━━━━━━━━━━━━━
분석팀 결론을 받기 전에 먼저 CIO 스스로의 관점을 정립한다.
분석팀 방향에 동의/부분동의/이견을 반드시 명시하고, 이견 시 그 근거를 밝힌다.
분석팀이 틀릴 수 있는 조건(멍거 역발상)을 항상 확인한다.

━━━━━━━━━━━━━━━━━━━━━━━━━━
[출력 원칙]
━━━━━━━━━━━━━━━━━━━━━━━━━━
- 판단 먼저, 근거는 짧게: "결론 → 이유" 순서, 설명이 결론을 앞서면 안 됨
- 포지션 결정은 반드시: 신규편입 / 비중확대 / 비중축소 / 청산 / 보유유지 명시
- 진입: "현 수준 ±X% 분할" (특정 가격 금지) | 손절: "thesis 훼손 또는 -X%"
- 불확실할 때 "판단 보류, 현금 유지"는 유효한 결정
- 한국어. 괄호 예시 텍스트 출력 금지. 지침 텍스트 출력 금지.
- 현재 수치 우선: [CIO 핵심 수치 팩트 시트]의 최신 수치가 최우선. 아카이브(과거 N일 추세)는 방향 파악 참고용에 불과함.
- 환율 방향 판단: 팩트 시트 USD/KRW change_pct가 음수(-)면 원화강세 = 외국인 유입 환경 (긍정 신호). 양수(+)면 원화약세 = 외국인 이탈 주의.
- 아카이브에 과거 고환율 데이터가 있어도 팩트 시트 현재가 하락 중이면 반드시 "원화강세·안정" 방향으로 분석할 것. "최근 환율 상승 어려움" 표현 금지.

[의사결정 로그 출력 규칙]
브리핑 마지막에 반드시 아래 블록을 출력 (파싱용, 텔레그램 메시지에서 제거됨):

=CIO_DECISION_START=
stance|[neutral/defensive/aggressive]|[현금목표%]
thesis|[intact/challenged/reconsider]
analyst|[agree/partial/disagree]|[이견 이유 — 없으면 공란]
new|[코드]|[종목명]|[비중%]|[high/medium/low]|[mid/long]|[테제 한 줄]|[early/developing/mature]|[손익비 예:3.5:1]|[반증신호 — 이것이 발생하면 thesis 훼손]
reduce|[코드]|[종목명]|[축소%]|[이유]
exit|[코드]|[종목명]||[이유]
hold|[코드]|[종목명]|[high/medium/low]|[early/developing/mature/exhausted]|[재검토 트리거]|[반증신호]
risk|[리스크 한 줄]
note|[전략 메모]
=CIO_DECISION_END=

# 없는 항목은 행 전체 생략. new/reduce/exit/hold는 해당 종목 수만큼 반복."""


def _build_pre_analysis_prompt() -> str:
    """Phase A: 팩트 시트 + 투자관만으로 CIO 독립 가설 수립 (분析팀 보고 전 단계)."""
    return """당신은 CIO입니다. 분析팀 보고서를 보기 전 단계입니다.
아래 [CIO 핵심 수치 팩트 시트]와 [월간 투자관]만 보고 독립적인 시장 가설을 수립하세요.
뉴스, 위원회 의견, 분析팀 보고서는 이 단계에서 참조하지 마세요.

출력 규칙:
- 5줄 이하로 작성
- 팩트 시트 실제 수치를 반드시 인용 (예시 수치 금지)
- 아래 형식으로 정확히 출력 (콜론 뒤에 실제 판단을 작성)

[CIO 선행 가설 — 분析팀 보고 전]
레짐: RISK-ON 또는 NEUTRAL 또는 RISK-OFF — 팩트 시트 수치 2가지 근거 (예: VIX 18.2↓, EWY +1.2%)
방향: 상승 또는 보합 또는 하락 — 핵심 수치 포함
역발상: 시장 컨센서스가 틀릴 수 있는 구체적 조건, 없으면 없음
핵심 리스크: 가장 우려되는 변수 하나와 임계값
포지션: 공격적 확대 또는 현상 유지 또는 방어적 축소"""


def _build_prompt_global() -> str:
    return f"""{_CIO_CHARTER}

━━━━━━━━━━━━━━━━━━━━━━━━━━
🌐 글로벌 시황 브리핑 (미국 장 마감 후)
━━━━━━━━━━━━━━━━━━━━━━━━━━

[출력 형식 — 팩트 시트 수치만 사용. 예시 텍스트 절대 출력 금지]

🧭 CIO 독립 판단 (팩트 시트 수치 기반 — 분析팀과 독립)
※ 컨텍스트의 [CIO 선행 가설 — 팩트 시트 독립 판단] 블록을 먼저 확인. 선행 가설 유지/수정 여부를 명시할 것.
레짐: [RISK-ON/NEUTRAL/RISK-OFF] — [팩트 시트 수치 3가지 근거]
내일 KOSPI: [우호/중립/불리] — EWY [수치]([+/-X%]) / SOX [+/-X%] 기반
선행 가설 vs 분析팀: [선행 가설 방향 유지 / 수정 — 분析팀 보고서 검토 후 변경 이유]
컨센서스와 다른 점: [시장이 현재 틀리고 있거나 놓치는 것 — 또는 "컨센서스 동의, 이유: ○○"]

🎯 종합 판단
투자관: [✅유지 / ⚠️도전 / 🔴재검토]
분석팀: [✅동의 / ⚠️부분동의 / 🔴이견] — [한 줄 근거 필수]

📌 포트폴리오 결정
[신규]: 종목명(코드) [비중]% | 단계:EARLY/DEVELOPING | R:R=[X:1](업↑X%/손↓Y%) | 반증: [○○이 발생하면]
[조정/청산]: 종목명(코드) [조치] — [thesis 기반 이유]
[없으면]: 변동 없음 — [현 포지션 유지 근거]

📊 미국 시장 팩트 (팩트 시트 수치 그대로)
S&P500 [수치]([+/-X.X%]) | NASDAQ [+/-X.X%] | SOX [수치]([+/-X.X%])
EWY [수치]([+/-X%]) → 내일 외국인 [매수기대/중립/매도우려]
USD/KRW [수치] | 미10Y [X.XX%] | VIX [수치]

⚠️ 내일 시나리오 (확률 합계 100%)
📈 기본 [X%]: [가장 가능성 높은 흐름] → [CIO 대응 방향]
🌟 우호 [Y%]: [상승 촉매 + 임계값] → [수혜 섹터]
🔻 비관 [Z%]: [하락 촉매 + 임계값] → [헤지 기준]
☠️ 꼬리위험: [저확률·고충격 — 발동 조건 한 줄]
━━━━━━━━━━━━━━━━━━━━━━━━━━

총 30줄 이하. 괄호·예시 텍스트 절대 출력 금지. 팩트 시트 수치만 인용."""


def _build_prompt_pre() -> str:
    return f"""{_CIO_CHARTER}

━━━━━━━━━━━━━━━━━━━━━━━━━━
📡 장전 브리핑
━━━━━━━━━━━━━━━━━━━━━━━━━━

[출력 형식 — 팩트 시트 수치만 사용. 예시 텍스트 절대 출력 금지]

🧭 CIO 독립 판단 (팩트 시트 수치 기반 — 분析팀과 독립)
※ 컨텍스트의 [CIO 선행 가설 — 팩트 시트 독립 판단] 블록을 먼저 확인. 선행 가설 유지/수정 여부 명시.
레짐: [RISK-ON/NEUTRAL/RISK-OFF] — [근거 수치 2가지]
오늘 예상: [갭업/보합/갭다운] — KOSPI200선물 [수치] / EWY [수치]([+/-X%]) 기반
선행 가설 검증: [선행 가설 방향 유지 / 수정 — 분析팀 보고서 검토 후 변경 이유]
컨센서스와 다른 점: [시장이 놓치는 것 — 또는 "동의, 이유: ○○"]

🎯 종합 판단
투자관: [✅유지 / ⚠️도전 / 🔴재검토]
분석팀: [✅동의 / ⚠️부분동의 / 🔴이견] — [한 줄 근거 필수]

📌 오늘 포트폴리오 액션
▶ 신규: 종목명(코드) X% | 단계:EARLY/DEVELOPING | R:R=[X:1](업↑X%/손↓Y%) | 확신 상/중/하 | 진입 현 수준 ±X% 분할 | 반증: [○○이 발생하면 thesis 훼손]
   없으면: 신규 없음
▶ 조정: 종목명 X%→Y% — [thesis 기반 이유] / 없으면 생략
▶ 유지: [종목 목록] | 단계:[EARLY/DEVELOPING/MATURE] | 반증: [각 종목의 훼손 신호]

📊 장전 핵심 팩트 (팩트 시트 수치 그대로)
KOSPI200선물: [수치] | S&P500선물: [수치]([+/-X.X%]) | SOX: [+/-X.X%]
EWY: [수치]([+/-X%]) → 외국인 [매수예상/중립/매도예상]
USD/KRW: [수치] (전일비 [+/-X원]) | 미10Y: [X.XX%] | VIX: [수치]

⚠️ 오늘 핵심 리스크
[구체적 리스크 한 줄 + 임계값 명시]

💡 CIO 한마디
[오늘 시장 핵심 판단 — 구체적 조건·수치 포함 1~2줄]
━━━━━━━━━━━━━━━━━━━━━━━━━━

총 30줄 이하. 괄호·예시 텍스트 절대 출력 금지. 팩트 시트 수치만 인용."""


def _build_prompt_close() -> str:
    return f"""{_CIO_CHARTER}

━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 장마감 브리핑
━━━━━━━━━━━━━━━━━━━━━━━━━━

[출력 규칙 — 필수 준수]
⚠️ 아래 각 섹션(🔍, 🧭, 🎯, 📌, 📊, ⚠️, 💡)을 순서대로 반드시 독립적으로 출력할 것. 섹션 병합·생략·순서 변경 금지.
팩트 시트·실제 수급 데이터만 사용. 예시 텍스트 절대 출력 금지.

🔍 자기점검 ([오늘 장전 CIO 예측] 컨텍스트 기반)
장전 예상 vs 실제: [장전에서 예상한 갭업/보합/갭다운] → KOSPI [수치]([+/-X%]) — [✅적중 / ❌불일치]
오판 원인: [정보오류 / 판단오류 / 외생충격 — 해당 항목 명시. 없으면 "없음"]

🧭 CIO 독립 판단 (팩트 시트 기반)
※ 컨텍스트의 [CIO 선행 가설] 블록(장전)과 [오늘 장전 CIO 예측 — 자기점검] 블록을 모두 참조.
오늘 시장 정의: [수치 3가지로 오늘을 한 줄로 표현]
레짐: [RISK-ON/NEUTRAL/RISK-OFF] | 컨센서스와 다른 점: [또는 "동의, 이유: ○○"]
선행 가설 정확도: [장전 선행 가설 방향이 실제와 일치/불일치 — 오판 원인 한 줄]

🎯 오늘 결론
KOSPI [팩트시트수치]([+/-X.X%]) | 외국인 [순매수/균형/순매도 — 팩트시트 TOP5 기반] | 기관 [순매수/순매도] | 주도: [섹터]
투자관: [✅강화 / ✅유지 / ⚠️도전 / 🔴재검토] — [오늘 수치 기반 이유]
분석팀: [✅동의 / ⚠️부분동의 / 🔴이견] — [한 줄 근거 필수]

📌 내일 포트폴리오 액션
▶ [신규]: 종목명(코드) X% | R:R=[X:1] | 단계:EARLY/DEVELOPING | 반증: [훼손 신호]
▶ [조정/청산]: 종목명(코드) [조치] — [이유]
▶ 없으면: 변동 없음 — [현 포지션 유지 근거]
▶ 유지 재확인: [종목 목록] | 단계 | 반증: [각 종목 훼손 신호]

📊 오늘 시장 핵심
오늘 이슈: [핵심 이슈] → [단발 / 트렌드] — [판단 근거]
섹터 흐름: 강세 [섹터+수치] | 약세 [섹터+수치]

⚠️ 내일 시나리오 (확률 합계 100%)
📈 기본 [X%]: [가장 가능성 높은 흐름] → [CIO 대응 방향]
🌟 우호 [Y%]: [상승 촉매 + 임계값] → [수혜 섹터·종목]
🔻 비관 [Z%]: [하락 촉매 + 임계값] → [헤지·현금 기준]
☠️ 꼬리위험: [저확률·고충격 시나리오 — 발동 조건]
야간 주목: [미국 이벤트] (한국시간 기준)

💡 CIO 한마디
[오늘 교훈 또는 내일 원칙 — 수치·구체적 조건 포함 1~2줄]
━━━━━━━━━━━━━━━━━━━━━━━━━━

총 35줄 이하. 괄호·예시 텍스트 절대 출력 금지. 실제 데이터·판단만 출력."""


def _build_prompt_intra1() -> str:
    return f"""{_CIO_CHARTER}

━━━━━━━━━━━━━━━━━━━━━━━━━━
🕙 장중 브리핑 (오전)
━━━━━━━━━━━━━━━━━━━━━━━━━━

[출력 규칙 — 필수 준수]
⚠️ 아래 각 섹션(🧭, 🎯, 📌, ⚠️, 💡)을 순서대로 반드시 독립적으로 출력할 것. 섹션 병합·생략·순서 변경 금지.
팩트 시트·실시간 수급 데이터만 사용. 예시 텍스트 절대 출력 금지.

🧭 CIO 오전 독립 점검
오전 팩트: KOSPI [팩트시트수치]([+/-X%]) | 외국인 [순매수/균형/순매도 — 팩트시트TOP5기반] | 주도: [섹터]
오전 해석: [장전 예상과 일치/불일치 — 원인 수치 포함]
오후 핵심 변수: [가장 중요한 것 한 가지 + 수치 기준]
컨센서스와 다른 점: [또는 "동의, 이유: ○○"]

🎯 오전 결론
투자관: [✅유지 / ⚠️수정] — [이유]
분석팀: [✅동의 / ⚠️부분동의 / 🔴이견] — [한 줄 근거 필수]

📌 오후 포트폴리오 행동
▶ [집행 / 보류 / 청산] — [종목·비중·이유 + 반증신호]
오후 주목: S&P500선물 [수치]([+/-X%]) | USD/KRW [수치] | 수혜 섹터: [섹터]

⚠️ 오후 핵심 리스크
[오후 주의사항 — 조건·수치 포함 한 줄]

💡 CIO 한마디
[오전 흐름 해석 + 오후 원칙 — 구체적 수치·조건 포함 1~2줄]
━━━━━━━━━━━━━━━━━━━━━━━━━━

총 25줄 이하. 괄호·예시 텍스트 절대 출력 금지. 팩트 시트 수치만 인용."""


def _build_prompt_intra2() -> str:
    return f"""{_CIO_CHARTER}

━━━━━━━━━━━━━━━━━━━━━━━━━━
🕐 장중 브리핑 (오후)
━━━━━━━━━━━━━━━━━━━━━━━━━━

[출력 규칙 — 필수 준수]
⚠️ 아래 각 섹션(🧭, 🎯, 📌, ⚠️, 💡)을 순서대로 반드시 독립적으로 출력할 것. 섹션 병합·생략·순서 변경 금지.
팩트 시트·실시간 수급 데이터만 사용. 예시 텍스트 절대 출력 금지.

🧭 CIO 오후 독립 판단
오늘 팩트: KOSPI [팩트시트수치]([+/-X%]) | 외국인 [순매수/균형/순매도 — 팩트시트TOP5기반] | 방향: [강세유지/박스권/약세전환]
오전과 달라진 것: [또는 "없음 — 오전 흐름 유지"]
반전 신호: [있으면 수치와 함께 / 없으면 "없음"]
컨센서스와 다른 점: [또는 "동의, 이유: ○○"]

🎯 오후 결론
투자관: [✅유지 / ⚠️수정] — [이유 수치 포함]
분석팀: [✅동의 / ⚠️부분동의 / 🔴이견] — [한 줄 근거 필수]

📌 마감 전 포트폴리오 행동
▶ [집행 / 보류] — [종목·비중·이유 + 반증신호]
▶ 내일 검토: [종목명 또는 "없음"]

⚠️ 오늘 이슈 & 내일 시나리오 (확률 합계 100%)
오늘 핵심: [이슈] → [단발 / 트렌드] — [판단 근거]
📈 기본 [X%]: [조건] | 🌟 우호 [Y%]: [촉매] | 🔻 비관 [Z%]: [임계값]
☠️ 꼬리위험: [저확률·고충격 발동 조건]
야간 주목: [미국 이벤트] (한국시간 기준)

💡 CIO 한마디
[추격·투기 금지 원칙 + 오늘 핵심 교훈 — 구체적 상황·수치 포함 1~2줄]
━━━━━━━━━━━━━━━━━━━━━━━━━━

총 28줄 이하. 괄호·예시 텍스트 절대 출력 금지. 팩트 시트 수치만 인용."""


# ── 메인 run() ───────────────────────────────────────────────────────────────

def run(state: InvestmentState) -> InvestmentState:
    try:
        run_type = state.get("run_type", RUN_TYPE_PRE)
        now      = datetime.now(TZ)
        date     = state.get("date", now.strftime("%Y-%m-%d"))

        # ── 컨텍스트 조립 ──────────────────────────────────────────────────
        context_parts: list[str] = [
            f"날짜: {date}  시간: {now.strftime('%H:%M')}",
            f"시장 방향성(분석팀): {state.get('market_direction', '중립')}",
        ]

        # 핵심 수치 팩트 시트 — LLM 환각 방지, 브리핑에 이 수치를 그대로 사용
        raw_mkt = state.get("raw_market_data", {})
        def _mval(k: str, f: str = "close"):
            return raw_mkt.get(k, {}).get(f)

        fact_rows = []
        for label, key, fld in [
            ("S&P500",       "sp500",          "close"),
            ("NASDAQ",       "nasdaq",         "close"),
            ("SOX",          "sox",            "close"),
            ("EWY(한국ETF)", "ewy",            "close"),
            ("USD/KRW",      "usd_krw",        "close"),
            ("미10Y(%)",     "us10y",          "close"),
            ("VIX",          "vix",            "close"),
            ("DXY",          "dxy",            "close"),
        ]:
            v = _mval(key, fld)
            if v is None:
                continue
            chg = _mval(key, "change_pct")
            chg_str = f" ({chg:+.2f}%)" if chg is not None else ""
            if key == "usd_krw" and chg is not None:
                fx_dir = "원화강세↑외국인유입✅" if chg < 0 else "원화약세↓외국인이탈주의⚠️"
                fact_rows.append(f"  {label}: {float(v):,.0f}{chg_str} ← {fx_dir}")
            else:
                fact_rows.append(f"  {label}: {v}{chg_str}")

        # 실시간 선물
        for label, key in [
            ("S&P500선물(오버나잇)", "sp500_futures"),
            ("나스닥선물(오버나잇)", "nasdaq_futures"),
            ("KOSPI200선물",        "kospi200_futures"),
        ]:
            d = raw_mkt.get(key, {})
            if d:
                rt_pct = d.get("realtime_pct")
                if rt_pct is not None:
                    fact_rows.append(f"  {label}: {d.get('close','')} (일봉 {d.get('change_pct',0):+.2f}% / 오버나잇 {rt_pct:+.2f}%)")
                else:
                    fact_rows.append(f"  {label}: {d.get('close','')} ({d.get('change_pct',0):+.2f}%)")

        # 실시간 한국 지수
        kr_rt = state.get("kr_index_realtime", {})
        for key, label in [("kospi", "KOSPI(실시간)"), ("kosdaq", "KOSDAQ(실시간)")]:
            d = kr_rt.get(key)
            if d:
                fact_rows.append(f"  {label}: {d['current']:,.2f} ({d['change_pct']:+.2f}%)")

        # 외국인·기관 수급 방향 (종목 순위 기반 — 합계 억원은 미제공)
        raw_kis = state.get("raw_kis_data", {})
        _frgn = [s.get("hts_kor_isnm") or s.get("name","")
                 for s in (raw_kis.get("kospi_foreign_rank",[])[:4]
                            + raw_kis.get("kosdaq_foreign_rank",[])[:3])]
        _frgn = [n for n in _frgn if n][:5]
        if _frgn:
            fact_rows.append(f"  외국인순매수TOP5(종목순위기반): {', '.join(_frgn)}")
        _inst = [s.get("hts_kor_isnm") or s.get("name","")
                 for s in (raw_kis.get("kospi_institution_rank",[])[:4]
                            + raw_kis.get("kosdaq_institution_rank",[])[:3])]
        _inst = [n for n in _inst if n][:5]
        if _inst:
            fact_rows.append(f"  기관순매수TOP5(종목순위기반): {', '.join(_inst)}")

        if fact_rows:
            context_parts.insert(0,
                "[CIO 핵심 수치 팩트 시트 — 브리핑에 이 수치를 그대로 인용]\n" + "\n".join(fact_rows)
            )

        # Phase B: 포지션 생애주기 현황 주입
        try:
            from services.position_lifecycle_service import get_lifecycle_context
            lc_ctx = get_lifecycle_context()
            if lc_ctx:
                context_parts.append("\n" + lc_ctx)
        except Exception as _lce:
            logger.debug("[CIO] 생애주기 주입 실패: %s", _lce)

        # Phase C: 예측 적중률 주입 (자기학습)
        try:
            from services.prediction_service import get_accuracy_summary
            acc_ctx = get_accuracy_summary(days=20)
            if acc_ctx:
                context_parts.append("\n" + acc_ctx)
        except Exception as _ace:
            logger.debug("[CIO] 적중률 주입 실패: %s", _ace)

        # 투자관 — 모든 판단의 최우선 기준
        thesis = state.get("investment_thesis", "")
        if thesis:
            context_parts.insert(1,
                "\n[월간 투자관 — CIO 판단의 헌법. 오늘 결정이 이와 정합하는지 ①에서 반드시 명시]\n"
                + thesis)

        # 주간 전략 프레임
        weekly = state.get("weekly_strategy_summary", "")
        if weekly:
            idx = 2 if thesis else 1
            context_parts.insert(idx,
                "\n[이번 주 전략 프레임 — 오늘 결정이 이 방향과 충돌하면 이유를 명시]\n"
                + weekly)

        # 분析팀 인텔리전스 — 시장 컨센서스 명시, CIO 독립 판단 우선 지시
        if state.get("committee_report"):
            context_parts.append(
                "\n[분析팀 인텔리전스 — 시장 컨센서스. 🧭 CIO 독립 판단 섹션을 이 보고서와 독립적으로 먼저 작성할 것]\n"
                + state["committee_report"]
            )

        # 포트폴리오 현황 (매니저 분석)
        if state.get("portfolio_report"):
            context_parts.append(
                "\n[포트폴리오 현황 + 매니저 의견 — CIO 최종 결정 전 참고]\n"
                + state["portfolio_report"]
            )

        # 매크로·리스크·인텔리전스
        for label, key in [
            ("[매크로 레짐 — 달리오 프레임]",        "macro_report"),
            ("[이벤트 리스크]",                      "event_risk_report"),
            ("[글로벌 전문가 서사]",                  "market_intelligence_report"),
            ("[리스크 관리팀]",                      "risk_report"),
        ]:
            if state.get(key):
                context_parts.append(f"\n{label}\n{state[key]}")

        # 최근 누적 데이터 (추세 파악)
        try:
            from services.market_archive_service import get_market_trend_context, get_intelligence_context
            trend_ctx = get_market_trend_context(days=7)
            if trend_ctx:
                context_parts.append(f"\n[최근 7일 시장 추세]\n{trend_ctx}")
            intel_ctx = get_intelligence_context(days=5)
            if intel_ctx:
                context_parts.append(f"\n[최근 인텔리전스 아카이브]\n{intel_ctx}")
        except Exception as _e:
            logger.debug("[CIO] 누적 데이터 주입 실패: %s", _e)

        # 급등종목 수급 교차
        try:
            surge_ctx = _format_surge_context(state.get("raw_kis_data", {}))
            if surge_ctx:
                context_parts.append("\n[급등종목 수급 교차분석]\n" + surge_ctx)
        except Exception as _e:
            logger.debug("[CIO] 급등종목 교차 주입 실패: %s", _e)

        # ── run_type별 추가 컨텍스트 ──────────────────────────────────────

        if run_type == RUN_TYPE_GLOBAL:
            us_hot = state.get("us_hot_stocks", [])
            if us_hot:
                context_parts.append(
                    "\n[미국 주요 종목 + 한국 공급망]\n" + format_us_impact_for_prompt(us_hot)
                )
            for label, key in [
                ("[미국 시장 분석]",        "us_market_report"),
                ("[글로벌 매크로]",          "global_market_report"),
                ("[미국발 한국 연동]",       "us_impact_report"),
                ("[빅피겨 발언]",            "bigfigure_report"),
                ("[야간 뉴스]",              "news_report"),
                ("[중장기 수혜주]",          "midterm_stock_report"),
            ]:
                if state.get(key):
                    context_parts.append(f"\n{label}\n{state[key]}")

        if run_type == RUN_TYPE_PRE:
            if state.get("futures_report"):
                context_parts.insert(2,
                    "\n[야간 선물·EWY·VIX — 오늘 갭 방향 핵심]\n" + state["futures_report"])
            for label, key in [
                ("[글로벌 매크로]",          "global_market_report"),
                ("[미국 시장]",              "us_market_report"),
                ("[미국발 한국 연동]",       "us_impact_report"),
                ("[섹터 분석]",              "sector_report"),
                ("[수급 분석]",              "money_flow_report"),
                ("[이슈 종목]",              "issue_stocks_report"),
                ("[빅피겨 발언]",            "bigfigure_report"),
                ("[뉴스]",                   "news_report"),
                ("[중장기 수혜주]",          "midterm_stock_report"),
            ]:
                if state.get(key):
                    context_parts.append(f"\n{label}\n{state[key]}")

            us_hot = state.get("us_hot_stocks", [])
            if us_hot:
                context_parts.append(
                    "\n[미국 시장 → 오늘 KOSPI 연동]\n" + format_us_impact_for_prompt(us_hot)
                )
            if state.get("dart_disclosures"):
                from agents.dart_alert_agent import format_disclosures_for_briefing
                dart_text = format_disclosures_for_briefing(state["dart_disclosures"])
                if dart_text:
                    context_parts.append("\n[DART 공시]\n" + dart_text)

            # 애널리스트 컨센서스 목표주가 (가치 평가 참고)
            try:
                from services.consensus_service import build_consensus_context, format_consensus_for_ceo
                consensus_raw = state.get("consensus_data", {})
                _raw     = consensus_raw.get("_raw", {})
                _namemap = consensus_raw.get("_name_map", {})
                if _raw:
                    kis_pre = KISClient()
                    _prices = {}
                    for _code in _raw:
                        try:
                            _pd = kis_pre.get_stock_price(_code, market=None)
                            if _pd.get("price"):
                                _prices[_code] = {"price": _pd["price"]}
                        except Exception:
                            pass
                    full_cons = build_consensus_context(list(_raw.keys()), _namemap, _prices, _raw)
                    if full_cons:
                        cons_text = format_consensus_for_ceo(full_cons)
                        if cons_text:
                            context_parts.append("\n[애널리스트 컨센서스 목표주가]\n" + cons_text)
            except Exception as _ce:
                logger.debug("[CIO] 컨센서스 주입 실패: %s", _ce)

        if run_type == RUN_TYPE_CLOSE:
            # 오늘 장전 CIO 예측 — 자기점검용
            try:
                from services.prediction_service import get_selfcheck_context as _gsc
                _sc_ctx = _gsc(date)
                if _sc_ctx:
                    context_parts.insert(2, "\n" + _sc_ctx)
                else:
                    # fallback: ceo_report 원문
                    from db.database import get_conn as _gc
                    from sqlalchemy import text as _t
                    with _gc() as _conn:
                        _pre_row = _conn.execute(
                            _t("SELECT ceo_report FROM reports "
                               "WHERE date=:d AND run_type='pre_market' "
                               "ORDER BY id DESC LIMIT 1"),
                            {"d": date}
                        ).fetchone()
                    if _pre_row and _pre_row[0]:
                        context_parts.insert(2,
                            "\n[오늘 장전 CIO 예측 — 자기점검: 예측 vs 실제 비교]\n"
                            + _pre_row[0][:600])
            except Exception as _pe:
                logger.debug("[CIO] 장전 예측 로드 실패: %s", _pe)

            # 자동 실행 결과
            try:
                from services.auto_execute_service import get_auto_execution_summary
                auto_summary = get_auto_execution_summary(days=7)
                if auto_summary:
                    context_parts.append("\n[이번 주 자동 실행 결과]\n" + auto_summary)
            except Exception:
                pass

            # 추천 성과 통계 (30일)
            try:
                stats = get_performance_stats(days=30)
                if stats["total"] >= 3:
                    context_parts.append(
                        f"\n[최근 30일 추천 성과]\n"
                        f"총 {stats['total']}건 | 승률 {stats['win_rate']}% | "
                        f"평균수익률 {stats['avg_return']:+.2f}% | 손익비 {stats['profit_factor']:.2f}\n"
                        f"→ 승률 50% 미만이면 확신도 '하' 결정 자제"
                    )
            except Exception:
                pass

            # NAV 현황
            try:
                from services.nav_service import get_latest_nav
                nav = get_latest_nav()
                if nav:
                    alpha_signal = "✅초과수익 중" if nav["alpha_ytd"] >= 0 else "⚠️시장 하회 중"
                    context_parts.append(
                        f"\n[포트폴리오 자산 성장 현황]\n"
                        f"연초대비: {nav['nav_pct_ytd']:+.2f}%  "
                        f"Alpha: {nav['alpha_ytd']:+.2f}%  {alpha_signal}\n"
                        f"오늘 총 손익: {nav['total_pnl_pct']:+.2f}%\n"
                        f"→ Alpha가 음수이면 전략 재검토 신호"
                    )
            except Exception:
                pass

            for label, key in [
                ("[한국 시장 움직임]",        "korea_spot_report"),
                ("[섹터·테마 흐름]",          "sector_report"),
                ("[수급 분석]",               "money_flow_report"),
                ("[이슈 종목]",               "issue_stocks_report"),
                ("[뉴스]",                    "news_report"),
                ("[빅피겨 발언]",             "bigfigure_report"),
                ("[중장기 수혜주]",           "midterm_stock_report"),
            ]:
                if state.get(key):
                    context_parts.append(f"\n{label}\n{state[key]}")

            if state.get("dart_disclosures"):
                from agents.dart_alert_agent import format_disclosures_for_briefing
                dart_text = format_disclosures_for_briefing(state["dart_disclosures"])
                if dart_text:
                    context_parts.append("\n[DART 공시 — 내일 포트폴리오 함의]\n" + dart_text)

            # 오늘 추천 종목 수익률
            try:
                kis_close = KISClient()
                results = update_close_prices(date, kis_close)
                returns_text = format_returns_for_report(results)
                context_parts.append(f"\n[오늘 추천 종목 수익률]\n{returns_text}")
            except Exception as e:
                logger.warning("[CIO] 종가 수집 실패: %s", e)

            # 장마감 컨센서스
            try:
                from services.consensus_service import build_consensus_context, format_consensus_for_ceo
                consensus_raw = state.get("consensus_data", {})
                _raw_c    = consensus_raw.get("_raw", {})
                _namemap_c= consensus_raw.get("_name_map", {})
                if _raw_c:
                    kis_close2 = KISClient()
                    _prices_c = {}
                    for _code_c in _raw_c:
                        try:
                            _pd_c = kis_close2.get_stock_price(_code_c, market=None)
                            if _pd_c.get("price"):
                                _prices_c[_code_c] = {"price": _pd_c["price"]}
                        except Exception:
                            pass
                    full_cons_c = build_consensus_context(list(_raw_c.keys()), _namemap_c, _prices_c, _raw_c)
                    if full_cons_c:
                        cons_text_c = format_consensus_for_ceo(full_cons_c)
                        if cons_text_c:
                            context_parts.append("\n[애널리스트 컨센서스 목표주가]\n" + cons_text_c)
            except Exception as _ce2:
                logger.debug("[CIO] 장마감 컨센서스 주입 실패: %s", _ce2)

        if run_type in (RUN_TYPE_INTRA1, RUN_TYPE_INTRA2):
            kr_rt = state.get("kr_index_realtime", {})
            if kr_rt:
                idx_lines = []
                for key, label in [("kospi", "KOSPI"), ("kosdaq", "KOSDAQ")]:
                    d = kr_rt.get(key)
                    if d:
                        idx_lines.append(
                            f"{label} {d['current']:,.2f} ({d['change_pct']:+.2f}%)"
                        )
                if idx_lines:
                    context_parts.append("\n[실시간 지수]\n" + "\n".join(idx_lines))

            for label, key in [
                ("[S&P·나스닥 선물]",    "futures_report"),
                ("[한국 시장 움직임]",   "korea_spot_report"),
                ("[섹터 흐름]",          "sector_report"),
                ("[이슈 종목]",          "issue_stocks_report"),
                ("[수급]",               "money_flow_report"),
                ("[뉴스]",               "news_report"),
            ]:
                if state.get(key):
                    context_parts.append(f"\n{label}\n{state[key]}")

            _bf = state.get("bigfigure_report", "")
            if _bf and _bf not in ("빅피겨 뉴스 없음", "빅피겨 주요 뉴스 없음", "[빅피겨 분석 일시 불가]"):
                context_parts.append("\n[빅피겨 발언]\n" + _bf)

            if state.get("dart_disclosures"):
                from agents.dart_alert_agent import format_disclosures_for_briefing
                dart_text = format_disclosures_for_briefing(state["dart_disclosures"])
                if dart_text:
                    context_parts.append("\n[DART 공시]\n" + dart_text)

        if state.get("review_report"):
            context_parts.append(f"\n[복기]\n{state['review_report']}")

        # ── 프롬프트 선택 ──────────────────────────────────────────────────
        if run_type == RUN_TYPE_GLOBAL:
            prompt = _build_prompt_global()
        elif run_type == RUN_TYPE_PRE:
            prompt = _build_prompt_pre()
        elif run_type == RUN_TYPE_CLOSE:
            prompt = _build_prompt_close()
        elif run_type == RUN_TYPE_INTRA1:
            prompt = _build_prompt_intra1()
        else:
            prompt = _build_prompt_intra2()

        # ── Phase A: CIO 선행 가설 수립 (팩트 시트만으로 독립 판단) ────────
        _pre_analysis = ""
        if run_type in (RUN_TYPE_PRE, RUN_TYPE_CLOSE, RUN_TYPE_GLOBAL):
            try:
                _pre_ctx_parts = [p for p in context_parts
                                  if any(k in p for k in (
                                      "팩트 시트", "투자관", "전략 프레임",
                                      "생애주기", "적중률",
                                  ))]
                _pre_ctx = "\n".join(_pre_ctx_parts)[:3000]
                if _pre_ctx:
                    _pre_analysis = chat_ceo(
                        _build_pre_analysis_prompt(), _pre_ctx, max_tokens=400
                    )
                    logger.info("[CIO] Phase A 선행 가설 수립 완료")
            except Exception as _pae:
                logger.debug("[CIO] Phase A 선행 가설 실패: %s", _pae)

        # 선행 가설을 분析팀 보고서 바로 앞에 삽입 (앵커링 방지)
        if _pre_analysis:
            _ctx_final = []
            _inserted = False
            for _part in context_parts:
                if not _inserted and "분析팀 인텔리전스" in _part:
                    _ctx_final.append(
                        "\n[CIO 선행 가설 — 팩트 시트 독립 판단 (아래 분析팀 보고서와 비교)]\n"
                        + _pre_analysis
                    )
                    _inserted = True
                _ctx_final.append(_part)
            if not _inserted:
                _ctx_final.insert(1,
                    "\n[CIO 선행 가설 — 팩트 시트 독립 판단]\n" + _pre_analysis
                )
            context = "\n".join(_ctx_final)
        else:
            context = "\n".join(context_parts)

        # ── LLM 호출 ──────────────────────────────────────────────────────
        raw_result = chat_ceo(prompt, context, max_tokens=1800)

        # ── CIO 결정 로그 파싱 + 텔레그램 메시지 정리 ──────────────────────
        ceo_report, ceo_decisions = _parse_cio_decisions(raw_result, date, run_type)
        state["ceo_report"]   = ceo_report
        state["ceo_decisions"]= ceo_decisions

        # Phase B: R/R 검증 — 3:1 미달 포지션 경고
        try:
            from services.position_lifecycle_service import check_rr_warnings
            _rr_warns = check_rr_warnings(ceo_decisions)
            if _rr_warns:
                for _w in _rr_warns:
                    logger.warning("[CIO] %s", _w)
                state["errors"].extend(_rr_warns)
        except Exception as _rre:
            logger.debug("[CIO] R/R 검증 실패: %s", _rre)

        # Phase B: 생애주기 DB 업데이트
        try:
            from services.position_lifecycle_service import update_from_cio_decisions
            update_from_cio_decisions(date, ceo_decisions)
        except Exception as _lce2:
            logger.debug("[CIO] 생애주기 업데이트 실패: %s", _lce2)

        # Phase C: 장전·글로벌 브리핑 예측 저장
        if run_type in (RUN_TYPE_PRE, RUN_TYPE_GLOBAL):
            try:
                from services.prediction_service import save_cio_prediction
                save_cio_prediction(date, run_type, ceo_report,
                                    state.get("raw_market_data", {}))
            except Exception as _pse:
                logger.debug("[CIO] 예측 저장 실패: %s", _pse)

        # Phase C: 장마감 → 실제 KOSPI 결과 기록
        if run_type == RUN_TYPE_CLOSE:
            try:
                from services.prediction_service import update_actual_result
                _kr_rt = state.get("kr_index_realtime", {})
                _k_chg = _kr_rt.get("kospi", {}).get("change_pct")
                if _k_chg is not None:
                    update_actual_result(date, float(_k_chg))
            except Exception as _are:
                logger.debug("[CIO] 실제결과 기록 실패: %s", _are)

        logger.info(
            "[CIO] 브리핑 완료 — 스탠스: %s | 현금목표: %d%% | 테제: %s | 신규: %d건 | 조정: %d건",
            ceo_decisions["macro_stance"],
            ceo_decisions["cash_target_pct"],
            ceo_decisions["thesis_status"],
            len(ceo_decisions["new_positions"]),
            len(ceo_decisions["position_changes"]),
        )

        # ── 장전: 포트폴리오 draft 등록 (ceo_decisions 기반) ──────────────
        if run_type == RUN_TYPE_PRE:
            _register_drafts(date, ceo_decisions)
            _trigger_auto_buy(ceo_decisions, state)

    except Exception as e:
        logger.error("[CIO] 실패: %s", e)
        state["ceo_report"]    = "브리핑 생성 실패"
        state["ceo_decisions"] = {}
        state["errors"].append(f"ceo_agent: {e}")
    return state


def _register_drafts(date: str, decisions: dict) -> None:
    """ceo_decisions의 new_positions를 portfolio_positions draft로 등록."""
    positions = decisions.get("new_positions", [])
    if not positions:
        return
    try:
        from db.database import get_conn
        from sqlalchemy import text as _text
        with get_conn() as conn:
            for pos in positions:
                code = pos.get("code", "")
                if not code:
                    continue
                exists = conn.execute(
                    _text("SELECT 1 FROM portfolio_positions "
                          "WHERE code=:c AND entry_date=:d AND status='draft'"),
                    {"c": code, "d": date},
                ).fetchone()
                if not exists:
                    tf_map = {"mid": "mid", "long": "long", "short": "short"}
                    conn.execute(_text("""
                        INSERT INTO portfolio_positions
                        (code, name, quantity, avg_price, entry_date, target_price, stop_price,
                         timeframe, memo, status)
                        VALUES (:code, :name, 0, 0, :date, 0, 0, :tf, :memo, 'draft')
                    """), {
                        "code": code,
                        "name": pos.get("name", ""),
                        "date": date,
                        "tf":   tf_map.get(pos.get("timeframe", "mid"), "mid"),
                        "memo": (f"CIO결정({date}): {pos.get('thesis','')[:200]} | "
                                 f"확신:{pos.get('conviction','medium')} | "
                                 f"비중목표:{pos.get('size_pct',0)}%"),
                    })
        logger.info("[CIO] portfolio draft %d건 등록", len(positions))
    except Exception as e:
        logger.warning("[CIO] draft 등록 실패: %s", e)


def _trigger_auto_buy(decisions: dict, state: dict) -> None:
    """자동매수 트리거 — AUTO_EXECUTE_BUY 설정 시에만 실행."""
    try:
        from config.settings import AUTO_EXECUTE_BUY
        if not AUTO_EXECUTE_BUY:
            return
        positions = decisions.get("new_positions", [])
        if not positions:
            return
        from services.auto_execute_service import auto_buy_recommendation
        from services.nav_service import get_latest_nav
        from clients.telegram_client import send_message as _tg_send

        nav         = get_latest_nav()
        total_assets= int(nav.get("total_value", 0)) if nav else 0

        executed, blocked = [], []
        for pos in positions:
            rec = {
                "code":  pos.get("code", ""),
                "name":  pos.get("name", ""),
                "rationale": pos.get("thesis", ""),
            }
            try:
                r = auto_buy_recommendation(rec, total_assets)
                (executed if r.get("success") else blocked).append(r)
            except Exception as _re:
                blocked.append({"success": False, "name": rec["name"],
                                 "code": rec["code"], "reason": str(_re)})

        if executed or blocked:
            lines = ["🤖 *CIO 자동 실행 결과*\n"]
            for r in executed:
                lines.append(f"✅ 매수: {r.get('name')}({r.get('code')}) "
                              f"{r.get('qty',0)}주 @{r.get('price',0):,}원")
            for r in blocked:
                lines.append(f"🚫 차단: {r.get('name')}({r.get('code')}) — {r.get('reason','')}")
            _tg_send("\n".join(lines))
    except ImportError:
        pass
    except Exception as _ae:
        logger.warning("[CIO] 자동 실행 트리거 실패: %s", _ae)
