"""
자기학습 시스템
매월 1일 지난달 복기 데이터 분석 → 가중치 저장 → 다음 달 투자위원회에 반영
"""
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from services.recommendation_service import get_recent_recommendations
from clients.openai_client import chat
from clients.telegram_client import send_message

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

_WEIGHTS_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "prompts", "learned_weights.md")
)


def _load_current_weights() -> str:
    try:
        with open(_WEIGHTS_PATH, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "(초기 데이터 없음)"


def _save_weights(content: str):
    os.makedirs(os.path.dirname(_WEIGHTS_PATH), exist_ok=True)
    with open(_WEIGHTS_PATH, "w", encoding="utf-8") as f:
        f.write(content)


def run_monthly_analysis() -> str:
    """지난달 성과 분석 → learned_weights.md 갱신 → 텔레그램 발송."""
    now  = datetime.now(_KST)
    recs = get_recent_recommendations(days=35)  # 지난달 포함

    if not recs:
        logger.info("[학습] 분석 데이터 없음")
        return ""

    rec_text = "\n".join(
        f"{r['date']} | {r['name']}({r['code']}) | "
        f"진입 {r.get('entry_price', 0):,}원 → 종가 {int(r.get('close_price') or 0):,}원 | "
        f"{r.get('return_pct', 0):+.1f}% [{r.get('result', '?')}]"
        for r in recs
    )
    current_weights = _load_current_weights()

    analysis_prompt = f"""지난달 AI 투자 추천 성과를 심층 분석하고, 다음 달에 반영할 구체적인 가중치를 제시하세요.

=== 추천 성과 데이터 ===
{rec_text}

=== 현재 가중치 ===
{current_weights}

분석 결과를 아래 형식으로 작성하세요:

## 분석 날짜: {now.strftime('%Y-%m-%d')}

### 성과 요약
- 총 추천: N건 / 성공: N건 / 실패: N건 / 평균 수익률: X%

### 잘 작동한 조건
1. ...
2. ...

### 작동하지 않은 조건
1. ...
2. ...

### 다음 달 적용 가중치 (구체적 수치 필수)
- 섹터 우선순위: (1위) ... (2위) ... (3위) ...
- 시장 조건별 전략: 상승장=... 하락장=...
- 스크리닝 조건: 등락률 기준 X%, 거래량 배율 Y배 이상
- 추천 종목 수: N개 (전 달 대비 조정)
- 손절/목표 비율 가이드: 손절 -X%, 목표 +Y%

### 투자위원회 프롬프트 반영 지시사항
(다음 달 투자위원회가 우선 고려해야 할 사항)
"""

    weights_md = chat("당신은 퀀트 투자 전략가입니다. 데이터 기반 분석만 하세요.", analysis_prompt, max_tokens=1500)
    _save_weights(weights_md)
    logger.info("[학습] learned_weights.md 업데이트 완료")

    # 요약 텔레그램 발송
    summary_lines = [l for l in weights_md.split("\n") if l.startswith("###") or l.startswith("-")][:15]
    summary = f"🧠 *AI 자기학습 월간 분석* ({now.strftime('%Y.%m')})\n\n" + "\n".join(summary_lines)
    send_message(summary)

    return weights_md


def load_learned_weights() -> str:
    """투자위원회에서 호출 — 현재 학습된 가중치 반환"""
    return _load_current_weights()
