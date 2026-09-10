"""agents/attribution_agent.py._save_attribution 점수 파싱 회귀 테스트 (2026-09-10).

_SYSTEM의 실제 출력형식은 "🏆 이번 주 점수: X/10"이지 "종합 점수"가 아니다 —
그 문구가 출력 어디에도 없어 total_score가 항상 0.0으로 저장됐다. thesis_s도
"투자관 부합"이라는 문구는 섹션⑤ 제목에만 있고 숫자가 안 따라와서, 그 뒤
한참 떨어진 "다음 주 개선 (최대 3개...)" 같은 무관한 숫자를 주웠다. 실제
_SYSTEM 요약 줄 형식("매크로 X | 섹터 X | 종목 X | 타이밍 X | 투자관 X")에
맞춰 라벨을 고쳤다. web/app.py가 attribution_log.total_score/thesis_score를
대시보드에 노출하므로 죽은 필드가 아니라 실제 소비처가 있다.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from db.database import get_conn, init_db
from sqlalchemy import text

import agents.attribution_agent as attribution_agent

_KST = ZoneInfo("Asia/Seoul")

_SAMPLE_REPORT = """🏆 이번 주 점수: 7/10
   매크로 8 | 섹터 6 | 종목 7 | 타이밍 6 | 투자관 8

① 시장 방향 판단: KOSPI 상승 예측 적중, 반도체 주도 정확히 짚음
② 섹터 선택: 2차전지 비중이 과도해 반도체 대비 언더퍼폼
③ 종목 선택: 삼성전자 선택은 좋았으나 진입 타이밍 아쉬움
④ 타이밍: 진입이 하루 늦어 상승분 일부 놓침
⑤ 투자관 부합: 이번주 추천은 현재 투자관 방향과 대체로 일치했다

🔧 다음 주 개선 (최대 3개, 각 1줄)
1. 2차전지 비중 축소 검토
2. 진입 타이밍을 하루 앞당길 것
3. 반도체 섹터 추가 발굴
"""


def setup_function(_):
    init_db()
    with get_conn() as conn:
        conn.execute(text("DELETE FROM attribution_log"))


def test_total_and_thesis_score_parsed_correctly():
    week_end = datetime.now(_KST).strftime("%Y-%m-%d")
    attribution_agent._save_attribution(week_end, _SAMPLE_REPORT)

    with get_conn() as conn:
        row = conn.execute(
            text("SELECT total_score, thesis_score, macro_score, sector_score, "
                 "stock_score, timing_score FROM attribution_log WHERE week_end=:w"),
            {"w": week_end},
        ).fetchone()

    assert row is not None
    total_score, thesis_score, macro_score, sector_score, stock_score, timing_score = row
    assert total_score == 7.0, "이번 주 점수(7)가 아니라 다른 값이 저장됨 — '종합 점수' 문구 부재로 0.0 버그 재발"
    assert thesis_score == 8.0, "투자관 점수(8)가 아니라 엉뚱한 숫자가 저장됨 — '투자관 부합' 라벨 버그 재발"
    assert macro_score == 8.0
    assert sector_score == 6.0
    assert stock_score == 7.0
    assert timing_score == 6.0
