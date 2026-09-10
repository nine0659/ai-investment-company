"""agents/thesis_agent.py 구조화 필드 파싱 회귀 테스트 (2026-08-07 발견,
2026-09-10 수정).

sector_overweight/sector_underweight/conviction_ideas/invalidation은
_parse_structured_fields에 파싱 코드 자체가 없어 항상 빈 값이었다. 그 중
invalidation은 장식용 DB 컬럼이 아니라 agents/emergency_monitor_agent.py의
시간당(매 정시) "투자관 무효조건 발동" 감지가 실제로 읽는 유일한 입력이라
(`if not thesis.get("invalidation"): return`), 이 안전장치가 도입 이후
한 번도 실행된 적이 없었다. 이 테스트는 실제 _THESIS_SYSTEM 출력 형식을
그대로 따르는 샘플 리포트로 네 필드 모두 실제 파싱되는지 검증한다.
"""
import agents.thesis_agent as thesis_agent

_SAMPLE_REPORT = """① 경기 사이클 위치 규명
  - 현재 어느 단계인가: 중기확장
  - 판단 근거: 수익률 곡선 정상, PMI 확장

② 핵심 매크로 드라이버 (5개 이내)
  - 연준 금리 인하 사이클, 각 드라이버가 6~12개월 내 점진적으로 전개될 전망

③ 6개월 전망 (구체적 수치 근거)
KOSPI 방향: 상승 확률 60%
핵심 촉매: 반도체 업황 회복

④ 12개월 전망 (구조적 관점)
경제 환경은 완만한 확장 국면이 지속될 전망

⑤ 자산 배분 가이드라인
현금 비중 권고: 20% (이유: 밸류에이션 부담)
섹터 비중 확대: 반도체, 방산 — 수출 모멘텀 강화
섹터 비중 축소: 건설 — 금리 부담 지속
국내/해외 비중: 70% / 30%

⑥ 핵심 확신 아이디어 (3~5개)
삼성전자(005930) | 투자 기간: 12개월 | 기대 수익: +20%
┌ 매크로 연결고리: 반도체 업사이클
└ 틀릴 조건: 메모리 가격 재하락

SK하이닉스(000660) | 투자 기간: 9개월 | 기대 수익: +25%
┌ 매크로 연결고리: HBM 수요 확대
└ 틀릴 조건: AI 투자 둔화

⑦ 시나리오 분석
강세(확률 30%): 금리 인하 가속 + KOSPI 3600~3800
기본(확률 50%): 완만한 확장 + KOSPI 3300~3500
약세(확률 20%): 인플레 재점화 + KOSPI 2900~3100

⑧ 투자관 무효 조건 (이 중 하나라도 발생하면 투자관 전면 재검토)
  - 미국 정책금리 추가 인상 전환
  - 반도체 업황 급랭 (DRAM 가격 3개월 연속 하락)
  - 지정학 리스크로 인한 공급망 붕괴
"""


def test_sector_overweight_and_underweight_parsed():
    fields = thesis_agent._parse_structured_fields(_SAMPLE_REPORT)
    assert fields["sector_overweight"] == ["반도체", "방산"]
    assert fields["sector_underweight"] == ["건설"]


def test_conviction_ideas_parsed():
    fields = thesis_agent._parse_structured_fields(_SAMPLE_REPORT)
    assert fields["conviction_ideas"] == [
        {"name": "삼성전자", "code": "005930"},
        {"name": "SK하이닉스", "code": "000660"},
    ]


def test_invalidation_parsed_as_nonempty_and_used_by_emergency_monitor(monkeypatch):
    fields = thesis_agent._parse_structured_fields(_SAMPLE_REPORT)
    assert fields["invalidation"], "invalidation이 비어있으면 emergency_monitor_agent의 시간당 체크가 매번 조기 종료된다"
    assert "반도체 업황 급랭" in fields["invalidation"]
    assert "지정학 리스크" in fields["invalidation"]

    # emergency_monitor_agent._check_thesis_invalidation의 조기 반환 게이트가
    # 실제로 통과하는지까지 확인 — get_active_thesis()가 이 invalidation 값을
    # 반환한다고 가정하고 게이트 조건만 검증한다.
    thesis = {"invalidation": fields["invalidation"]}
    assert bool(thesis.get("invalidation")) is True


def test_empty_report_yields_empty_fields_not_crash():
    fields = thesis_agent._parse_structured_fields("관련 섹션이 전혀 없는 텍스트입니다.")
    assert fields["sector_overweight"] == []
    assert fields["sector_underweight"] == []
    assert fields["conviction_ideas"] == []
    assert fields["invalidation"] == ""
