def score_stock(stock: dict, sector_scores: list[dict] | None = None,
               chart_score: float = 0.0) -> float:
    """종목 점수화 (0~100). chart_score는 chart_service 분석 결과."""
    score = 0.0
    sector_map = {s["sector"]: s["score"] for s in (sector_scores or [])}

    # 등락률 (최대 25점)
    chg = _f(stock.get("prdy_ctrt") or stock.get("change_pct", 0))
    if chg > 0:
        score += min(chg * 2.5, 25)

    # 거래량 비율 (최대 25점)
    vol = _f(stock.get("vol_inrt") or stock.get("vol_ratio", 0))
    if vol > 0:
        score += min(vol * 0.25, 25)

    # 차트 점수 (최대 30점)
    score += min(chart_score * 0.3, 30)

    # 섹터 모멘텀 (최대 10점)
    sector = stock.get("bstp_kor_isnm", stock.get("sector", ""))
    if sector in sector_map:
        score += sector_map[sector] * 0.1

    # 시가총액 적정 (최대 10점)
    mcap = _f(stock.get("lstg_cblc_qty", 0))
    if 0 < mcap < 1e10:
        score += 10
    elif 0 < mcap < 5e10:
        score += 7

    return round(min(score, 100), 1)


def score_sector(stocks: list[dict]) -> float:
    if not stocks:
        return 0.0
    avg_chg = sum(_f(s.get("prdy_ctrt", 0)) for s in stocks) / len(stocks)
    avg_vol = sum(_f(s.get("vol_inrt", 0)) for s in stocks) / len(stocks)
    return round(min(avg_chg * 5 + avg_vol * 0.1, 100), 1)


def _f(val) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0
