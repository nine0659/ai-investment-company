import logging
from graph.state import InvestmentState
from clients.openai_client import chat

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 금융 뉴스 분석 전문가입니다.

핵심 원칙: 미국에서 발생한 경제·정책·기업 이슈는 한국 시장에 직접 파급됩니다.
우선순위: 미국 뉴스 1순위 → 지정학 리스크 2순위 → 국내 기관수급 3순위 → 기타 글로벌·한국 뉴스.

[분석 항목]
1. [미국 핵심 뉴스] Fed 발언·금리·고용·CPI·기업 실적 → 한국 파급 영향 명시
2. [지정학 리스크 — 반드시 확인]
   - 중동: 이란·이스라엘·미군기지 공격·호르무즈 해협 긴장 → 원유 급등 → 에너지株, 운임, 항공 영향
   - 러시아·우크라이나: 에너지 공급 차질 → 원자재주·방산주 수혜
   - 북한 도발: KOSPI 직접 충격 → 방산주 단기 수혜
   - 미중 갈등: 반도체·공급망 규제 → 삼성전자·SK하이닉스 직접 영향
   - 해당 이슈가 뉴스에 없으면 "지정학 리스크 이슈 없음 (안정)"으로 명시
3. [국내 기관·외국인 수급 — 반드시 확인]
   - 국민연금: 주식 비율 조정·대규모 리밸런싱 → KOSPI 수급 충격 직접 요인
   - 연기금·공적자금: 매도 집중 기간 → 시장 하방 압력
   - 외국인: 원달러 환율 급등(1,400원↑) → 외국인 매도 가속화
   - 해당 이슈가 뉴스에 없으면 "기관·외국인 수급 이슈 없음"으로 명시
4. [한국 정책·기업 뉴스] 금감원·기재부·산업부 정책, 실적 서프라이즈/쇼크
5. 오늘 시장 전체 뉴스 센티먼트 (긍정/부정/혼조)

[출력 형식]
- [미국발 핵심 재료] TOP3 (제목 + 한국 영향 한 줄)
- [지정학 리스크] 감지된 이슈 또는 "이슈 없음" — 파급 섹터 명시
- [국내 기관·외국인 수급] 감지된 이슈 또는 "이슈 없음" — KOSPI 영향 명시
- [기타 글로벌·한국 보조 재료] TOP2
- [뉴스 기반 오늘 주목 섹터] (긍정/부정 구분)
- 전체 뉴스 센티먼트: 긍정·부정·혼조 + 한 줄 근거"""


def run(state: InvestmentState) -> InvestmentState:
    try:
        news = state.get("raw_news_data", {})
        lines = []
        for source, items in news.items():
            for item in items[:5]:
                if t := item.get("title"):
                    lines.append(f"[{source}] {t}")
        text = "\n".join(lines[:40]) if lines else "뉴스 없음"
        result = chat(_SYSTEM, f"오늘의 뉴스 헤드라인:\n{text}", max_tokens=2000)
        state["news_report"] = result
        logger.info("[뉴스팀] 완료")
    except Exception as e:
        logger.error("[뉴스팀] 실패: %s", e)
        state["news_report"] = "뉴스 수집 실패"
        state["errors"].append(f"news_team: {e}")
    return state
