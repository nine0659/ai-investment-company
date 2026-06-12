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
from datetime import datetime, time as _time
from graph.state import InvestmentState
from clients.openai_client import chat_ceo
from clients.kis_client import KISClient
from clients.market_data_client import fetch_kr_stock_technicals
from clients.us_stock_client import format_us_impact_for_prompt

from services.recommendation_service import (
    update_close_prices, format_returns_for_report, get_performance_stats,
)
from config.settings import RUN_TYPE_GLOBAL, RUN_TYPE_PRE, RUN_TYPE_INTRA1, RUN_TYPE_INTRA2, RUN_TYPE_CLOSE, TZ

_MARKET_OPEN  = _time(9, 0)
_MARKET_CLOSE = _time(15, 35)
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
_LOG_START = "=CIO_DECISION_START="
_LOG_END   = "=CIO_DECISION_END="
_LOG_RE    = re.compile(r"=CIO_DECISION_START=\n(.*?)\n=CIO_DECISION_END=", re.DOTALL)


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
                    "code":        parts[1],
                    "name":        parts[2],
                    "size_pct":    float(parts[3]),
                    "conviction":  parts[4],
                    "timeframe":   parts[5] if len(parts) > 5 else "mid",
                    "thesis":      parts[6] if len(parts) > 6 else "",
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
                    "review_trigger":  parts[4] if len(parts) > 4 else "",
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

━━━━━━━━━━━━━━━━━━━━━━━━━━
[분석팀 독립 검토 원칙]
━━━━━━━━━━━━━━━━━━━━━━━━━━
분석팀 결론을 받기 전에 먼저 CIO 스스로의 관점을 정립한다.
분석팀 방향에 동의/부분동의/이견을 반드시 명시하고, 이견 시 그 근거를 밝힌다.
분석팀이 틀릴 수 있는 조건(멍거 역발상)을 항상 확인한다.

━━━━━━━━━━━━━━━━━━━━━━━━━━
[의사결정 출력 규칙]
━━━━━━━━━━━━━━━━━━━━━━━━━━
- 모든 브리핑은 "오늘 어떤 의사결정을 내렸는가"로 시작
- 포지션 결정: 신규편입 / 비중확대 / 비중축소 / 청산 / 보유유지 중 하나를 명시
- 진입 표현: "현 수준 ±X% 이내 분할 접근" (특정 가격 지정 금지)
- 손절 표현: "thesis 훼손 또는 -X%" (기계적 정밀 가격 금지)
- 목표 표현: "+X~Y%" 또는 "컨센서스 목표가 기준" (단기 이익실현 타이밍 금지)
- 불확실할 때: "판단 보류, 현금 비중 유지"가 유효한 결정
- 오늘 변동성 이벤트를 단기 매매로 대응하는 것은 투기이며 CIO 역할 밖
- 텔레그램 한국어. 총 35줄 이하. 지침 텍스트 절대 출력 금지.

[의사결정 로그 출력 규칙]
브리핑 마지막에 반드시 아래 블록을 출력 (파싱용, 텔레그램 메시지에서 제거됨):

=CIO_DECISION_START=
stance|[neutral/defensive/aggressive]|[현금목표%]
thesis|[intact/challenged/reconsider]
analyst|[agree/partial/disagree]|[이견 이유 — 없으면 공란]
new|[코드]|[종목명]|[비중%]|[high/medium/low]|[mid/long]|[테제 한 줄]
reduce|[코드]|[종목명]|[축소%]|[이유]
exit|[코드]|[종목명]||[이유]
hold|[코드]|[종목명]|[high/medium/low]|[재검토 트리거]
risk|[리스크 한 줄]
note|[전략 메모]
=CIO_DECISION_END=

# 없는 항목은 행 전체 생략. new/reduce/exit/hold는 해당 종목 수만큼 반복."""


def _build_prompt_global() -> str:
    return f"""{_CIO_CHARTER}

━━━━━━━━━━━━━━━━━━━━━━━━━━
🌐 CIO 글로벌 시황 브리핑 (미국 장 마감 후)
━━━━━━━━━━━━━━━━━━━━━━━━━━

[출력 순서]

① 투자 테제 점검
투자관: [✅유지 / ⚠️도전 / 🔴재검토] — 오늘 글로벌 데이터가 테제를 어떻게 바꾸는가
⚖️ 분석팀 검토: [✅동의 / ⚠️부분동의 / 🔴이견] — [달리오·멍거·버핏 관점 핵심 이유]

② 오늘 포트폴리오 의사결정 (없으면 "변동 없음" 명시)
📌 결정: [신규편입 / 비중조정 / 청산 / 보유유지] 종목명(코드)
   근거: [구조적 이유 — 모멘텀·단기 재료가 아닌 thesis 기반]
   진입 구간: [현 수준 ±X% 분할] | 보유 기간: [X~X개월]
   손절 기준: [thesis 훼손 또는 -X%]

③ 매크로 레짐 판단 (달리오)
레짐: [RISK-ON/NEUTRAL/RISK-OFF] — 달러·금리·신용스프레드·유동성 근거
포지션 크기 방향: [공격적 / 보통 / 보수적] — 이유 한 줄

④ 오늘 밤 주목 공급망 신호
미국 강세 섹터 → 한국 수혜 구조 (아직 미반영 가능성)
내일 KOSPI 예상 환경: [우호적/중립/불리] — EWY·SOX 근거

⑤ 역발상 점검 (멍거)
지금 시장이 당연하다고 믿는 것 한 가지 + CIO가 보는 반대 시나리오

━━━━━━━━━━━━━━━━━━━━━━━━━━
⚡ CIO 핵심 판단 3줄
▶ [오늘 글로벌 신호가 우리 포트폴리오 전략에 미치는 함의]
📐 [내일·이번 주 중장기 포지션 방향]
🧭 [지금 가장 중요한 리스크 관리 원칙]
━━━━━━━━━━━━━━━━━━━━━━━━━━"""


def _build_prompt_pre() -> str:
    return f"""{_CIO_CHARTER}

━━━━━━━━━━━━━━━━━━━━━━━━━━
📡 CIO 장전 브리핑
━━━━━━━━━━━━━━━━━━━━━━━━━━

[출력 순서]

① 투자 테제 점검 + 분석팀 검토
투자관: [✅유지 / ⚠️도전 / 🔴재검토] — 오늘 데이터가 테제를 어떻게 바꾸는가
⚖️ 분석팀 검토: [✅동의 / ⚠️부분동의 / 🔴이견] — [달리오·멍거·버핏 핵심 이유]
시장 환경: 선물[방향] | S&P[방향] | SOX[방향] | EWY[방향]
오늘 예상: [갭업/보합/갭다운] — 근거: [선물·수급 신호]

② 오늘 CIO 포트폴리오 의사결정 (핵심 섹션)
📌 신규 편입 결정: (없으면 "신규 편입 없음")
   종목명(코드) | 비중 X% | 확신 [상/중/하] | 보유 기간 [X~X개월]
   근거: [구조적 이유 — 매크로 사이클 + 수급 + 밸류에이션]
   진입 구간: [현 수준 ±X% 내 분할] | 손절: [thesis 훼손 또는 -X%]
   목표: [+X~Y% 또는 컨센서스 기준]

📌 비중 조정: (없으면 생략)
   종목명(코드) [현재X% → 목표Y%] — [이유: 구조적 근거]

✅ 보유 유지:
   종목명(코드) — [유지 근거] | 재검토 조건: [thesis 훼손 시점]

③ 오늘 핵심 이슈 — 구조적 의미
이슈: [가장 중요한 이슈]
시장 의미: [단발 / 트렌드 시작 / 구조적 변화] — 판단 근거
포트폴리오 함의: [오늘 결정에 어떻게 반영했는가]

④ 중장기 종목 발굴 (3~12개월 관점)
주목 종목: 종목명(코드) — [업황 사이클 근거] | [아직 미반영 이유] | [편입 검토 조건]

━━━━━━━━━━━━━━━━━━━━━━━━━━
⚡ CIO 핵심 판단
▶ 단기: [오늘 시장 방향 + 포트폴리오 행동 방향]
📐 중장기: [오늘 움직임이 우리 투자 여정에서 신호인가, 소음인가]
🧭 원칙: [지금 가장 중요한 한 가지 투자 원칙]
━━━━━━━━━━━━━━━━━━━━━━━━━━"""


def _build_prompt_close() -> str:
    return f"""{_CIO_CHARTER}

━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 CIO 장마감 브리핑
━━━━━━━━━━━━━━━━━━━━━━━━━━

[출력 순서]

① 투자 테제 복기 + 분석팀 검토
투자관: [✅강화 / ✅유지 / ⚠️도전 / 🔴재검토] — 오늘 시장 데이터가 테제를 어떻게 바꾸는가
⚖️ 분석팀 검토: [✅동의 / ⚠️부분동의 / 🔴이견] — [달리오·멍거·버핏 핵심 이유]
오늘 수급: 외국인[순매수/도]XXX억 | 기관[순매수/도]XXX억 | 주도 섹터: [섹터]

② 오늘 CIO 포트폴리오 의사결정 (핵심 섹션)
📌 내일 편입/조정 결정: (없으면 "변경 없음")
   종목명(코드) | 비중 X% | 확신 [상/중/하] | 보유 기간 [X~X개월]
   근거: [오늘 시장에서 확인된 thesis 근거]
   진입 구간: [현 수준 ±X% 내 분할] | 손절: [thesis 훼손 또는 -X%]

📌 비중 조정 / 청산: (없으면 생략)
   종목명(코드) → [조치] — [이유: 구조적 근거]

✅ 보유 유지 재확인:
   종목명(코드) — [오늘 데이터 기준 thesis 유효성 재확인]

③ 오늘 시장 구조적 해석
오늘 핵심: [오늘 시장에서 가장 중요한 구조적 사실 한 줄]
오늘 이슈: [이슈] → [단발 / 트렌드] — [공급망·섹터 함의]
내일 시나리오: A [강세 조건] / B [약세 조건] | 야간 주목: [미국 발표·선물]

④ 중장기 종목 업데이트 (3~12개월)
오늘 근거: 종목명(코드) — [오늘 확인된 thesis 강화/약화 근거]

━━━━━━━━━━━━━━━━━━━━━━━━━━
⚡ CIO 핵심 판단
▶ 단기: [내일 시장 방향 + 포트폴리오 행동]
📐 중장기: [오늘 이슈가 우리 투자 여정의 방향을 바꾸는가]
🧭 원칙: [오늘 하루 기억해야 할 한 가지 투자 원칙]
━━━━━━━━━━━━━━━━━━━━━━━━━━"""


def _build_prompt_intra1() -> str:
    return f"""{_CIO_CHARTER}

━━━━━━━━━━━━━━━━━━━━━━━━━━
🕙 CIO 장중 브리핑 (오전)
━━━━━━━━━━━━━━━━━━━━━━━━━━

[출력 순서]

⚡ 오전 결론 → 장전 결정 [✅유지 / ⚠️수정 필요 / 🔴번복]
⚖️ 분석팀 검토: [✅동의 / ⚠️부분동의 / 🔴이견] — 이유 한 줄

① 오전 포트폴리오 점검
KOSPI[수치]([+/-X%]) | 주도 섹터: [섹터] | 외국인[순매수/도] 기관[순매수/도]
장전 판단 vs 실제: [✅일치 / ❌불일치] — 원인 한 줄
포지션 상태: 오늘 편입 결정 종목 진입 여부 + 보유 종목 이상 신호 유무

② 오전 이슈 해석 — 포트폴리오 함의
오전 주요 이슈: [이슈 + 단발 / 트렌드 판단]
포트폴리오 영향: [오늘 결정을 번복할 근거가 되는가? 아니면 소음인가]
오후 주목: S&P선물[방향] | 수혜 섹터 | KOSPI 레벨 체크

━━━━━━━━━━━━━━━━━━━━━━━━━━
🔔 CIO 오후 판단
▶ [오전 흐름 요약 + 오후 포지션 행동 방향]
📐 [오전 움직임이 중장기 포지션 전략을 바꾸는가]
🧭 [지금 감정을 다스리는 한 마디 — 원칙 기반]
━━━━━━━━━━━━━━━━━━━━━━━━━━"""


def _build_prompt_intra2() -> str:
    return f"""{_CIO_CHARTER}

━━━━━━━━━━━━━━━━━━━━━━━━━━
🕐 CIO 장중 브리핑 (오후)
━━━━━━━━━━━━━━━━━━━━━━━━━━

[출력 순서]

⚡ 오후 방향 → [강세유지 / 박스권 / 약세전환]
S&P선물[방향] | 외국인 오전[순매수/도]XXX억
⚖️ 분석팀 검토: [✅동의 / ⚠️부분동의 / 🔴이견] — 이유 한 줄

① 오전 복기 + 오후 포트폴리오 판단
KOSPI[수치]([+/-X%]) | 오전 주도 섹터: [섹터]
오늘 결정 유지 여부: [✅유지 / ⚠️수정] — 이유 한 줄
마감 전 행동: [오늘 편입 계획 집행 여부 + 포지션 이상 없음 확인]

② 오늘 이슈 종합 — 내일 포트폴리오 함의
오늘 핵심 이슈: [이슈] → [단발 / 트렌드] — [섹터·공급망 시사점]
⚠️ 마감 전 체크: [오늘 흐름이 내일 포지션 판단에 미치는 구조적 함의]

━━━━━━━━━━━━━━━━━━━━━━━━━━
🔔 CIO 마감 핵심 3줄
▶ [오늘 시장이 우리 포트폴리오 전략에 말하는 것]
📐 [오늘 새롭게 확인된 구조적 기회 또는 리스크]
❌ [마감 전 추격·투기 경계 — 원칙 기반]
━━━━━━━━━━━━━━━━━━━━━━━━━━"""


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

        # 분석팀 인텔리전스 (투위 결론 → 이제 팩트 정리만)
        if state.get("committee_report"):
            context_parts.append(
                "\n[분석팀 인텔리전스 — CIO 독립 검토 대상]\n"
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

        context = "\n".join(context_parts)

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

        # ── LLM 호출 ──────────────────────────────────────────────────────
        raw_result = chat_ceo(prompt, context, max_tokens=1800)

        # ── CIO 결정 로그 파싱 + 텔레그램 메시지 정리 ──────────────────────
        ceo_report, ceo_decisions = _parse_cio_decisions(raw_result, date, run_type)
        state["ceo_report"]   = ceo_report
        state["ceo_decisions"]= ceo_decisions

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
