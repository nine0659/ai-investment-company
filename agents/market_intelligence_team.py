"""
agents/market_intelligence_team.py
글로벌 투자 인텔리전스 분석 — 시장 전문가의 해석·서사·컨센서스를 추출

[기존 팀과의 역할 분리]
  news_analysis_team   : 국내 뉴스 헤드라인 → "무슨 일이 있었나" (팩트 레이어)
  bigfigure_agent      : 핵심 인사 발언 수집
  market_intelligence_team: 글로벌 전문가 해석 → "시장은 이걸 어떻게 보는가" (서사 레이어)

[가치]
  - 시장 서사(Narrative): 지금 시장을 움직이는 지배적 스토리
  - 강세론/약세론 캠프 구분: 양측 논리 요약
  - 컨센서스 변화 감지: 전문가 시각이 어떻게 바뀌고 있는가
  - 섹터별 전문가 뷰: AI·반도체·매크로·중국·한국 수급
  - 한국 시장 적용 인사이트
"""
import logging
from graph.state import InvestmentState
from clients.openai_client import chat
from clients.intelligence_client import fetch_all_intelligence
from clients.telegram_intelligence_client import (
    fetch_telegram_intelligence,
    format_for_context as fmt_telegram,
)
from clients.securities_report_client import (
    fetch_securities_reports,
    format_for_context as fmt_reports,
)
from clients.blog_scraper_client import (
    fetch_blog_posts,
    format_for_context as fmt_blog_posts,
)

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 글로벌 투자 인텔리전스 분석가입니다.
수집된 해외 전문가 분석·애널리스트 해설·시장 논평을 읽고,
지금 시장 참여자들이 어떤 '서사(Narrative)'로 시장을 보고 있는지를 추출하세요.

[핵심 임무]
"어떤 일이 있었다"가 아니라 "시장 전문가들이 이것을 어떻게 해석하는가"를 도출합니다.
단순 사실 요약 금지. 해석·논리·심리·컨센서스를 추출하세요.

[출력 형식 — 반드시 이 구조로]

📖 오늘의 지배적 시장 서사
→ [한 문장: 지금 글로벌 전문가 논의의 중심축이 무엇인가]

🐂 강세론 캠프
- 핵심 논리: [구체적 근거 1~2가지]
- 지지 진영: [어떤 기관/애널리스트/매체가 이 시각]

🐻 약세론 캠프
- 핵심 논리: [구체적 근거 1~2가지]
- 지지 진영: [어떤 기관/애널리스트/매체가 이 시각]

🔑 오늘 반복 등장 핵심 키워드
→ [전문가 논의에서 가장 많이 등장한 키워드 5개 — 쉼표 구분]

📡 섹터별 전문가 뷰
- AI/반도체 : [현재 전문가 다수 시각 — 긍정/중립/부정 + 핵심 논거]
- 매크로/금리: [금리 경로·Fed 경로에 대한 현 컨센서스]
- 중국/신흥국: [중국 경기·외국인 자금 흐름에 대한 글로벌 시각]
- 한국/외국인: [KOSPI 외국인 수급에 관한 글로벌 전문가 시각]

⚡ 컨센서스 변화 신호
→ [이전 대비 달라지고 있는 전문가 시각 — 없으면 "뚜렷한 변화 없음"]
→ 방향: [강화 / 약화 / 전환] + 이유 한 줄

💡 오늘 KOSPI 적용 인사이트
→ [위 글로벌 서사가 오늘 한국 시장에 미치는 함의 — 구체적으로 1~2가지]

[주의사항]
- 수집된 기사 제목/요약 기반 분석 — 확인 안 된 수치 단정 금지
- 전문가·기관 이름이 나오면 구체적으로 인용
- "~할 것 같다" → "~를 지지하는 전문가 시각이 우세" 형태로 표현
- 국내 투자 블로그 포스트(직접 수집)가 있으면 국내 개인 투자자 시각으로 별도 반영
- 인텔리전스 부족 시 "데이터 부족으로 판단 유보" 명시"""


def run(state: InvestmentState) -> InvestmentState:
    try:
        intel      = fetch_all_intelligence(max_per_source=4)
        tg_msgs    = fetch_telegram_intelligence()    # 설정 미완료면 빈 리스트
        reports    = fetch_securities_reports()       # 네이버 금융 증권사 리포트
        blog_posts = fetch_blog_posts()               # 등록 블로그 직접 수집

        lines: list[str] = ["=== 글로벌 투자 인텔리전스 피드 ==="]

        # 분류별 RSS 피드 (weight 높은 소스 우선 정렬 유지)
        feeds = intel.get("feeds", {})
        for category, items in feeds.items():
            if not items:
                continue
            lines.append(f"\n[{category}]")
            for item in items[:6]:
                src     = item.get("source_name", "")
                title   = item.get("title", "")
                summary = item.get("summary", "")
                lines.append(f"  [{src}] {title}")
                if summary:
                    lines.append(f"    → {summary[:180]}")

        # 전문가 관점 쿼리 결과
        expert = intel.get("expert_queries", {})
        if expert:
            lines.append("\n=== 전문가 관점 검색 결과 ===")
            for key, items in expert.items():
                if not items:
                    continue
                lines.append(f"\n[{key}]")
                for item in items[:3]:
                    lines.append(f"  {item.get('title', '')}")
                    if item.get("summary"):
                        lines.append(f"    → {item['summary'][:150]}")

        # 국내 투자 블로그 (Google News site:blog.naver.com 필터)
        blogs = intel.get("blog_queries", {})
        if blogs:
            lines.append("\n=== 국내 투자 블로그 분석 ===")
            for key, items in blogs.items():
                if not items:
                    continue
                lines.append(f"\n[{key}]")
                for item in items[:4]:
                    lines.append(f"  {item.get('title', '')}")
                    if item.get("summary"):
                        lines.append(f"    → {item['summary'][:150]}")

        # 증권사 리포트 (네이버 금융)
        if reports:
            report_text = fmt_reports(reports)
            if report_text:
                lines.append(f"\n{report_text}")

        # 국내 투자 블로그 직접 수집 포스트
        if blog_posts:
            blog_text = fmt_blog_posts(blog_posts)
            if blog_text:
                lines.append(f"\n{blog_text}")
            logger.info("[인텔리전스팀] 블로그직접수집 %d건 포함", len(blog_posts))

        # 텔레그램 채널 인텔리전스 (설정된 경우)
        if tg_msgs:
            tg_text = fmt_telegram(tg_msgs, max_per_category=8)
            if tg_text:
                lines.append(f"\n{tg_text}")
            logger.info("[인텔리전스팀] 텔레그램 %d건 포함", len(tg_msgs))

        # 빅피겨 리포트를 참조 컨텍스트로 추가 (이미 앞 단계에서 생성됨)
        if state.get("bigfigure_report"):
            lines.append(f"\n=== 빅피겨 발언 참조 (교차검증용) ===\n{state['bigfigure_report'][:600]}")

        # 매크로 레짐을 서사 분석 기준으로 추가
        if state.get("macro_report"):
            lines.append(f"\n=== 매크로 레짐 참조 ===\n{state['macro_report'][:300]}")

        context = "\n".join(lines)
        result  = chat(_SYSTEM, context, max_tokens=1200)
        state["market_intelligence_report"] = result

        logger.info(
            "[인텔리전스팀] 완료 - 피드 %d카테고리, 전문가쿼리 %d개, 블로그RSS %d개, 증권사리포트 %d건, 텔레그램 %d건, 블로그직접수집 %d건",
            len(feeds), len(expert), len(blogs), len(reports), len(tg_msgs), len(blog_posts),
        )
    except Exception as e:
        logger.error("[인텔리전스팀] 실패: %s", e)
        state["market_intelligence_report"] = "인텔리전스 수집 실패"
        state["errors"].append(f"market_intelligence_team: {e}")
    return state
