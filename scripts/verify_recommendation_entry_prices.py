"""scripts/verify_recommendation_entry_prices.py

stock_recommendations의 entry_price가 그 날짜 실제 시세와 맞는지 독립된
데이터 소스(yfinance)로 배치 검증한다.

recs_from_weekly_picks(services/recommendation_service.py)는 entry_price를
브리핑 텍스트가 아니라 항상 그 시점 price_lookup(KIS 시세) 값으로 덮어써
LLM이 지어낸 가격은 절대 저장 안 되도록 막는다 — 이건 파서 단위 테스트
(tests/test_recommendation_parser.py)가 이미 검증한다.

이 스크립트가 확인하는 건 다른 층위다: price_lookup의 출처인 KIS 시세
자체가 그 시점에 맞았는가. 파서 로직만으로는 "KIS가 틀린 값을 정직하게
반영한 경우"를 못 잡는다 — 독립 소스와 대조해야 드러난다.

사용법:
    python scripts/verify_recommendation_entry_prices.py
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))

_MISMATCH_THRESHOLD_PCT = 3.0


def _historical_close(code: str, date_str: str) -> float | None:
    """해당 날짜 시점 KIS get_stock_price()가 반환했을 값과 비교 가능한 종가를
    yfinance에서 조회 — 즉 그 날짜 "이전(포함)" 가장 최근 거래일 종가.

    recs_from_weekly_picks의 진입가는 주간 추천이 도는 일요일 20:00 시점
    KIS 최신가(=직전 거래일인 금요일 종가)다. "이후" 방향으로 찾으면 주말·
    공휴일 낀 recommendation_date에서 월요일 종가를 잘못 비교 대상으로
    잡아 며칠치 가격 변동을 데이터 오류로 오인한다(2026-08 최초 실행에서
    이 방향 버그로 SK하이닉스 18.2% "불일치"가 났다가, 방향을 고치니
    실제로는 일치했다 — 스크립트 자체의 회귀 케이스로 남겨둔다).
    """
    import yfinance as yf

    target = datetime.strptime(date_str, "%Y-%m-%d")
    start = target - timedelta(days=10)  # 연휴 대비 여유
    end   = target + timedelta(days=1)   # yfinance end는 배타적 — 당일 포함시키려면 +1
    for suffix in (".KS", ".KQ"):
        try:
            hist = yf.Ticker(code + suffix).history(
                start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d")
            )
            if not hist.empty:
                return float(hist["Close"].iloc[-1])  # 대상 날짜 이전 가장 최근 거래일
        except Exception:
            continue
    return None


def main() -> int:
    from db.database import engine
    from sqlalchemy import text

    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id, date, code, name, entry_price FROM stock_recommendations ORDER BY date")
        ).fetchall()

    if not rows:
        print("검증 대상 없음 — stock_recommendations이 비어있음")
        return 0

    print(f"검증 대상 {len(rows)}건 (임계값: {_MISMATCH_THRESHOLD_PCT}%)\n")

    skipped = []
    mismatches = []
    for rid, date, code, name, entry_price in rows:
        hist_price = _historical_close(code, date)
        if hist_price is None or not entry_price:
            print(f"[스킵] {name}({code}) {date}: 과거 시세 조회 실패 (yfinance)")
            skipped.append((name, code, date))
            continue

        diff_pct = abs(entry_price - hist_price) / hist_price * 100
        ok = diff_pct <= _MISMATCH_THRESHOLD_PCT
        print(
            f"[{'OK' if ok else '불일치'}] {name}({code}) {date}: "
            f"저장값 {entry_price:,.0f}원 vs yfinance {hist_price:,.0f}원 (차이 {diff_pct:.1f}%)"
        )
        if not ok:
            mismatches.append((name, code, date, entry_price, hist_price, diff_pct))

    print(f"\n총 {len(rows)}건 | 스킵 {len(skipped)}건 | 불일치 {len(mismatches)}건")
    if mismatches:
        print("\n⚠️ 아래는 저장된 진입가와 독립 소스(yfinance)가 "
              f"{_MISMATCH_THRESHOLD_PCT}% 이상 어긋남 — KIS 시세 소스 자체를 의심할 것:")
        for name, code, date, entry, hist, diff in mismatches:
            print(f"  {name}({code}) {date}: {entry:,.0f}원 vs {hist:,.0f}원 ({diff:.1f}%)")
        return 1

    print("\n불일치 없음 — 저장된 진입가가 독립 소스와 일치")
    return 0


if __name__ == "__main__":
    sys.exit(main())
