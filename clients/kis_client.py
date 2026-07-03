import logging
import time as _time_module
import requests
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from config.settings import (
    KIS_APP_KEY, KIS_APP_SECRET, KIS_BASE_URL,
    KIS_ACCOUNT_NO, KIS_ACCOUNT_PROD_CD, KIS_IS_REAL,
)

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")
# KIS 순위 API는 평일 07:00~20:00 KST에만 데이터 제공
_RANK_START = time(7, 0)
_RANK_END   = time(20, 0)

# 토큰 만료 전 갱신 버퍼 (만료 15분 전 미리 갱신)
_TOKEN_BUFFER_MINUTES = 15

# 서킷 브레이커: 토큰 발급이 전 재시도 실패하면 이 시간 동안 모든 KIS 호출을
# 즉시 실패시킨다. KIS 접속 불가 시 호출마다 3회×15초 재시도가 반복되면
# 파이프라인 전체가 15분을 넘겨 GitHub Actions 타임아웃으로 브리핑이 통째로
# 유실된다 (2026-07-02~03 장전브리핑 미발송 원인).
_CIRCUIT_COOLDOWN = timedelta(minutes=10)
_circuit_open_until: datetime | None = None


def _rank_api_available() -> bool:
    now = datetime.now(_KST)
    if now.weekday() >= 5:
        return False
    if not (_RANK_START <= now.time() <= _RANK_END):
        return False
    try:
        from utils.market_calendar import is_krx_trading_day
        if not is_krx_trading_day(now.date()):
            logger.debug("KIS 순위 API — 공휴일(%s) 스킵", now.strftime("%Y-%m-%d"))
            return False
    except ImportError:
        pass
    return True


class KISClient:
    def __init__(self):
        self._token: str | None = None
        self._token_expires: datetime | None = None

    # ── 인증 ─────────────────────────────────────────────────

    def _get_token(self) -> str:
        global _circuit_open_until
        # 만료 15분 전 버퍼를 두고 갱신 판단
        buffer = timedelta(minutes=_TOKEN_BUFFER_MINUTES)
        now_local = datetime.now(_KST)
        if self._token and self._token_expires and now_local < self._token_expires - buffer:
            return self._token

        if _circuit_open_until and now_local < _circuit_open_until:
            raise RuntimeError(
                f"KIS 서킷 브레이커 열림 — {_circuit_open_until.strftime('%H:%M')}까지 호출 차단 "
                "(직전 토큰 발급 전체 실패)"
            )

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                if attempt > 0:
                    wait = 2 ** attempt  # 2초, 4초 지수 백오프
                    logger.info("KIS 토큰 재시도 %d/3 (%d초 대기)", attempt + 1, wait)
                    _time_module.sleep(wait)

                r = requests.post(
                    f"{KIS_BASE_URL}/oauth2/tokenP",
                    json={"grant_type": "client_credentials",
                          "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET},
                    timeout=15,
                )

                # rate limit(429) 또는 일시적 서버 오류(5xx)는 재시도
                if r.status_code in (429, 500, 502, 503, 504):
                    logger.warning("KIS 토큰 발급 일시 오류 (HTTP %d), 재시도", r.status_code)
                    last_exc = requests.HTTPError(f"HTTP {r.status_code}")
                    continue

                r.raise_for_status()
                data = r.json()
                self._token = data["access_token"]

                # KIS 응답의 실제 만료 시간 사용 (expires_in: 초 단위, 기본 86400=24시간)
                expires_in = int(data.get("expires_in", 86400))
                if expires_in < 3600:  # 1시간 미만이면 기본값(24h) 사용
                    logger.warning("KIS expires_in 비정상(%ds) — 기본값 86400s 사용", expires_in)
                    expires_in = 86400
                self._token_expires = datetime.now(_KST) + timedelta(seconds=expires_in)
                _circuit_open_until = None
                logger.info(
                    "KIS 토큰 갱신 완료 (유효 %dh, 만료 %s)",
                    expires_in // 3600,
                    self._token_expires.strftime("%Y-%m-%d %H:%M"),
                )
                return self._token

            except requests.HTTPError as e:
                last_exc = e
                logger.warning("KIS 토큰 발급 HTTP 오류 (시도 %d/3): %s", attempt + 1, e)
            except Exception as e:
                last_exc = e
                logger.warning("KIS 토큰 발급 실패 (시도 %d/3): %s", attempt + 1, e)

        _circuit_open_until = datetime.now(_KST) + _CIRCUIT_COOLDOWN
        logger.error(
            "KIS 토큰 발급 3회 모두 실패 — 서킷 브레이커 %d분 가동 (이후 KIS 호출 즉시 실패)",
            int(_CIRCUIT_COOLDOWN.total_seconds() // 60),
        )
        raise RuntimeError(f"KIS 토큰 발급 3회 모두 실패: {last_exc}")

    def _headers(self, tr_id: str) -> dict:
        return {
            "content-type":  "application/json; charset=UTF-8",
            "authorization": f"Bearer {self._get_token()}",
            "appkey":        KIS_APP_KEY,
            "appsecret":     KIS_APP_SECRET,
            "tr_id":         tr_id,
            "custtype":      "P",
        }

    # ── 순위 조회 ─────────────────────────────────────────────

    def get_volume_rank(self, market: str = "J", top_n: int = 20) -> list[dict]:
        """거래량 순위 (J=KOSPI, Q=KOSDAQ)"""
        return self._rank(
            "/uapi/domestic-stock/v1/quotations/volume-rank",
            "FHPST01710000",
            {"FID_COND_MRKT_DIV_CODE": market, "FID_COND_SCR_DIV_CODE": "20171",
             "FID_INPUT_ISCD": "0000", "FID_DIV_CLS_CODE": "0", "FID_BLNG_CLS_CODE": "0",
             "FID_TRGT_CLS_CODE": "111111111", "FID_TRGT_EXLS_CLS_CODE": "000000",
             "FID_INPUT_PRICE_1": "", "FID_INPUT_PRICE_2": "", "FID_VOL_CNT": "", "FID_INPUT_DATE_1": ""},
            top_n,
        )

    def get_amount_rank(self, market: str = "J", top_n: int = 20) -> list[dict]:
        """거래대금 순위 (KOSPI: J, KOSDAQ: Q).
        KOSDAQ은 거래대금 전용 TR이 없으므로 volume-rank TR을 거래대금 정렬로 재사용.
        """
        # KOSDAQ은 TR_ID FHPST01710000 + FID_DIV_CLS_CODE=1(거래대금순)으로 대체
        if market == "Q":
            return self._rank(
                "/uapi/domestic-stock/v1/quotations/volume-rank",
                "FHPST01710000",
                {"FID_COND_MRKT_DIV_CODE": market, "FID_COND_SCR_DIV_CODE": "20171",
                 "FID_INPUT_ISCD": "0000", "FID_DIV_CLS_CODE": "1",
                 "FID_BLNG_CLS_CODE": "0",
                 "FID_TRGT_CLS_CODE": "111111111", "FID_TRGT_EXLS_CLS_CODE": "000000",
                 "FID_INPUT_PRICE_1": "", "FID_INPUT_PRICE_2": "", "FID_VOL_CNT": "",
                 "FID_INPUT_DATE_1": ""},
                top_n,
            )
        return self._rank(
            "/uapi/domestic-stock/v1/quotations/volume-rank",
            "FHPST01720000",
            {"FID_COND_MRKT_DIV_CODE": market, "FID_COND_SCR_DIV_CODE": "20172",
             "FID_INPUT_ISCD": "0000", "FID_DIV_CLS_CODE": "0", "FID_BLNG_CLS_CODE": "0",
             "FID_TRGT_CLS_CODE": "111111111", "FID_TRGT_EXLS_CLS_CODE": "000000",
             "FID_INPUT_PRICE_1": "", "FID_INPUT_PRICE_2": "", "FID_VOL_CNT": "",
             "FID_INPUT_DATE_1": "", "FID_RANK_SORT_CLS_CODE": "0"},
            top_n,
        )

    def get_fluctuation_rank(self, market: str = "J", rise: bool = True, top_n: int = 20) -> list[dict]:
        """등락률 순위"""
        return self._rank(
            "/uapi/domestic-stock/v1/ranking/fluctuation",
            "FHPST01760000",
            {"FID_COND_MRKT_DIV_CODE": market, "FID_COND_SCR_DIV_CODE": "20170",
             "FID_INPUT_ISCD": "0000", "FID_DIV_CLS_CODE": "1" if rise else "2",
             "FID_BLNG_CLS_CODE": "0", "FID_TRGT_CLS_CODE": "111111111",
             "FID_TRGT_EXLS_CLS_CODE": "000000",
             "FID_INPUT_PRICE_1": "", "FID_INPUT_PRICE_2": "", "FID_VOL_CNT": "", "FID_INPUT_DATE_1": ""},
            top_n,
        )

    def _rank(self, path: str, tr_id: str, params: dict, top_n: int) -> list[dict]:
        if not _rank_api_available():
            now = datetime.now(_KST)
            logger.debug(
                "KIS 순위 API 가용 시간 외 스킵 (%s) — 현재 %s KST (평일 07:00~20:00만 제공)",
                tr_id, now.strftime("%H:%M"),
            )
            return []
        url = f"{KIS_BASE_URL}{path}"
        try:
            r = requests.get(url, headers=self._headers(tr_id), params=params, timeout=10)
            if r.status_code == 404:
                logger.warning(
                    "KIS 순위 API 404 (%s) — 엔드포인트: %s | 응답: %s",
                    tr_id, url, r.text[:200],
                )
                return []
            r.raise_for_status()
            return r.json().get("output", [])[:top_n]
        except Exception as e:
            logger.warning("KIS 순위 조회 실패 (%s): %s", tr_id, e)
            return []

    def get_foreign_buy_rank(self, market: str = "J", top_n: int = 20) -> list[dict]:
        """외국인 순매수 상위 종목"""
        return self._rank(
            "/uapi/domestic-stock/v1/quotations/foreign-institution-total",
            "FHPTJ04400000",
            {"FID_COND_MRKT_DIV_CODE": market, "FID_COND_SCR_DIV_CODE": "20444",
             "FID_INPUT_ISCD": "0000", "FID_DIV_CLS_CODE": "0",
             "FID_RANK_SORT_CLS_CODE": "0",   # 0=외국인순매수
             "FID_ETC_CLS_CODE": "0",
             "FID_INPUT_DATE_1": "", "FID_INPUT_DATE_2": ""},
            top_n,
        )

    def get_institution_buy_rank(self, market: str = "J", top_n: int = 20) -> list[dict]:
        """기관 순매수 상위 종목"""
        return self._rank(
            "/uapi/domestic-stock/v1/quotations/foreign-institution-total",
            "FHPTJ04400000",
            {"FID_COND_MRKT_DIV_CODE": market, "FID_COND_SCR_DIV_CODE": "20444",
             "FID_INPUT_ISCD": "0000", "FID_DIV_CLS_CODE": "0",
             "FID_RANK_SORT_CLS_CODE": "1",   # 1=기관순매수
             "FID_ETC_CLS_CODE": "0",
             "FID_INPUT_DATE_1": "", "FID_INPUT_DATE_2": ""},
            top_n,
        )

    # ── 개별 종목 조회 ────────────────────────────────────────────

    def get_stock_price(self, stock_code: str, market: str | None = None) -> dict:
        """현재가·PER·PBR·EPS·BPS·시가총액 조회.

        market: "J"(KOSPI) 또는 "Q"(KOSDAQ).
        None이면 J→Q 순으로 자동 시도.
        지정 시 해당 market 먼저, 가격이 0이면 반대 market 재시도.
        """
        url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
        if market:
            markets = (market, "Q" if market == "J" else "J")
        else:
            markets = ("J", "Q")

        for m in markets:
            try:
                r = requests.get(
                    url,
                    headers=self._headers("FHKST01010100"),
                    params={"FID_COND_MRKT_DIV_CODE": m, "FID_INPUT_ISCD": stock_code},
                    timeout=10,
                )
                r.raise_for_status()
                o = r.json().get("output", {})

                def _int(k):   return int(float(o.get(k) or 0))
                def _float(k): return float(o.get(k) or 0)

                # 장 마감·개장 전에는 stck_prpr=0 → 전일 종가 fallback
                price = _int("stck_prpr") or _int("stck_prdy_clpr")
                if price == 0:
                    continue  # 빈 응답 → 다음 market 시도
                return {
                    "price":          price,
                    "per":            _float("per"),
                    "pbr":            _float("pbr"),
                    "eps":            _int("eps"),
                    "bps":            _int("bps"),
                    "issued_shares":  _int("lstg_stqt"),
                    "market_cap_억":  _int("hts_avls"),
                    "52w_high":       _int("d250_hgpr"),
                    "52w_low":        _int("d250_lwpr"),
                    "change_pct":     _float("prdy_ctrt"),
                }
            except Exception as e:
                logger.warning("KIS 주가 조회 실패 (%s/%s): %s", stock_code, m, e)
        return {}

    # ── 계좌 유틸 ─────────────────────────────────────────────────

    def _account(self) -> tuple[str, str]:
        """(CANO 8자리, ACNT_PRDT_CD 2자리) 반환."""
        raw = KIS_ACCOUNT_NO.replace("-", "")
        cano = raw[:8]
        prod = raw[8:10] if len(raw) >= 10 else KIS_ACCOUNT_PROD_CD
        return cano, prod

    def _get_hashkey(self, body: dict) -> str:
        """KIS POST 주문에 필요한 hashkey 발급."""
        try:
            r = requests.post(
                f"{KIS_BASE_URL}/uapi/hashkey",
                headers={
                    "content-type": "application/json; charset=UTF-8",
                    "appkey": KIS_APP_KEY,
                    "appsecret": KIS_APP_SECRET,
                },
                json=body,
                timeout=10,
            )
            r.raise_for_status()
            return r.json().get("HASH", "")
        except Exception as e:
            logger.debug("hashkey 발급 실패 (무시): %s", e)
            return ""

    def _tr(self, side: str) -> tuple[str, str]:
        """side('buy'|'sell') → (TR_ID_매수, TR_ID_매도) 반환."""
        if side == "buy":
            return ("TTTC0802U" if KIS_IS_REAL else "VTTC0802U")
        return ("TTTC0801U" if KIS_IS_REAL else "VTTC0801U")

    # ── 잔고·보유 조회 ─────────────────────────────────────────────

    def get_account_balance(self) -> dict:
        """예수금·보유 종목 잔고 조회.
        반환: {
          'cash': 예수금(원),
          'total_eval': 평가금액(원),
          'holdings': [{code, name, qty, avg_price, eval_price, pnl_pct}, ...]
        }
        """
        cano, prod = self._account()
        tr_id = "TTTC8434R" if KIS_IS_REAL else "VTTC8434R"
        url   = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance"
        params = {
            "CANO": cano, "ACNT_PRDT_CD": prod,
            "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
            "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        }
        try:
            r = requests.get(url, headers=self._headers(tr_id), params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            out1 = data.get("output1", [])   # 보유 종목 리스트
            out2 = data.get("output2", {})   # 계좌 요약

            def _i(d, k): return int(float(d.get(k) or 0))
            def _f(d, k): return float(d.get(k) or 0)

            holdings = []
            for s in out1:
                qty = _i(s, "hldg_qty")
                if qty <= 0:
                    continue
                holdings.append({
                    "code":       s.get("pdno", ""),
                    "name":       s.get("prdt_name", ""),
                    "qty":        qty,
                    "avg_price":  _i(s, "pchs_avg_pric"),
                    "eval_price": _i(s, "prpr"),
                    "eval_amt":   _i(s, "evlu_amt"),
                    "pnl_amt":    _i(s, "evlu_pfls_amt"),
                    "pnl_pct":    _f(s, "evlu_pfls_rt"),
                })

            summary = out2[0] if isinstance(out2, list) and out2 else out2
            return {
                "cash":          _i(summary, "dnca_tot_amt") if isinstance(summary, dict) else 0,
                "total_eval":    _i(summary, "tot_evlu_amt") if isinstance(summary, dict) else 0,
                "purchase_amt":  _i(summary, "pchs_amt_smtl_amt") if isinstance(summary, dict) else 0,
                "holdings":      holdings,
            }
        except Exception as e:
            logger.warning("KIS 잔고 조회 실패: %s", e)
            return {"cash": 0, "total_eval": 0, "purchase_amt": 0, "holdings": []}

    def get_holdings(self) -> list[dict]:
        """보유 종목 리스트만 반환 (get_account_balance 래퍼)."""
        return self.get_account_balance().get("holdings", [])

    # ── 주문 ─────────────────────────────────────────────────────

    def place_order(
        self,
        code:  str,
        side:  str,   # "buy" | "sell"
        qty:   int,
        price: int = 0,  # 0 = 시장가, 양수 = 지정가
    ) -> dict:
        """국내주식 현금 주문.
        반환: {
          'success': bool,
          'order_no': 주문번호,
          'message': 설명,
          'mode': 'real' | 'paper'
        }
        """
        cano, prod = self._account()
        tr_id      = self._tr(side)
        ord_dvsn   = "00" if price > 0 else "01"  # 00=지정가, 01=시장가
        body = {
            "CANO":         cano,
            "ACNT_PRDT_CD": prod,
            "PDNO":         code,
            "ORD_DVSN":     ord_dvsn,
            "ORD_QTY":      str(qty),
            "ORD_UNPR":     str(price),
        }
        hashkey = self._get_hashkey(body)
        headers = self._headers(tr_id)
        if hashkey:
            headers["hashkey"] = hashkey

        url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
        try:
            r = requests.post(url, headers=headers, json=body, timeout=15)
            r.raise_for_status()
            data = r.json()
            rt_cd  = data.get("rt_cd", "")
            msg    = data.get("msg1", "")
            out    = data.get("output", {})
            order_no = out.get("ODNO", "")
            success = rt_cd == "0"
            mode = "real" if KIS_IS_REAL else "paper"
            logger.info("[KIS주문] %s %s %d주 @%s — %s (%s) 주문번호:%s",
                        side, code, qty, price or "시장가", "성공" if success else "실패",
                        mode, order_no)
            return {"success": success, "order_no": order_no, "message": msg, "mode": mode}
        except Exception as e:
            logger.error("[KIS주문] 실패: %s", e)
            return {"success": False, "order_no": "", "message": str(e), "mode": "error"}

    # ── 미체결 주문 ────────────────────────────────────────────────

    def get_pending_orders(self) -> list[dict]:
        """미체결 주문 리스트 조회."""
        cano, prod = self._account()
        tr_id = "TTTC8036R" if KIS_IS_REAL else "VTTC8036R"
        url   = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"
        params = {
            "CANO": cano, "ACNT_PRDT_CD": prod,
            "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
            "INQR_DVSN_1": "", "INQR_DVSN_2": "0",
        }
        try:
            r = requests.get(url, headers=self._headers(tr_id), params=params, timeout=10)
            r.raise_for_status()
            items = r.json().get("output", [])
            result = []
            for o in items:
                qty_left = int(float(o.get("psbl_qty") or o.get("ord_qty") or 0))
                if qty_left <= 0:
                    continue
                result.append({
                    "order_no":  o.get("odno", ""),
                    "code":      o.get("pdno", ""),
                    "name":      o.get("prdt_name", ""),
                    "side":      "buy" if o.get("sll_buy_dvsn_cd") == "02" else "sell",
                    "qty":       qty_left,
                    "price":     int(float(o.get("ord_unpr") or 0)),
                    "ordered_at": o.get("ord_tmd", ""),
                })
            return result
        except Exception as e:
            logger.warning("KIS 미체결 조회 실패: %s", e)
            return []

    def cancel_order(
        self,
        org_order_no: str,
        code: str,
        side: str,   # "buy" | "sell"
        qty:  int,
        price: int = 0,
    ) -> dict:
        """미체결 주문 취소."""
        cano, prod = self._account()
        tr_id    = "TTTC0803U" if KIS_IS_REAL else "VTTC0803U"
        sll_buy  = "02" if side == "buy" else "01"
        ord_dvsn = "00" if price > 0 else "01"
        body = {
            "CANO":         cano,
            "ACNT_PRDT_CD": prod,
            "KRX_FWDG_ORD_ORGNO": "",
            "ORGN_ODNO":    org_order_no,
            "ORD_DVSN":     ord_dvsn,
            "RVSE_CNCL_DVSN_CD": "02",  # 02=취소
            "ORD_QTY":      str(qty),
            "ORD_UNPR":     str(price),
            "QTY_ALL_ORD_YN": "Y",
            "PDNO":         code,
            "SLL_BUY_DVSN_CD": sll_buy,
        }
        hashkey = self._get_hashkey(body)
        headers = self._headers(tr_id)
        if hashkey:
            headers["hashkey"] = hashkey

        url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/order-rvsecncl"
        try:
            r = requests.post(url, headers=headers, json=body, timeout=15)
            r.raise_for_status()
            data = r.json()
            success  = data.get("rt_cd") == "0"
            order_no = data.get("output", {}).get("ODNO", "")
            return {"success": success, "order_no": order_no, "message": data.get("msg1", "")}
        except Exception as e:
            logger.warning("KIS 주문 취소 실패: %s", e)
            return {"success": False, "order_no": "", "message": str(e)}

    def get_dividend_info(self, stock_code: str) -> dict:
        """배당 정보 조회 (배당수익률)"""
        url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/finance/dividend"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code}
        try:
            r = requests.get(url, headers=self._headers("FHKST01010600"), params=params, timeout=10)
            r.raise_for_status()
            o = r.json().get("output", {})
            return {
                "dividend_per_share": float(o.get("per_sto_divi_amt") or 0),
                "dividend_yield":     float(o.get("stck_divi") or 0),
            }
        except Exception as e:
            logger.debug("KIS 배당 조회 실패 (%s): %s", stock_code, e)
            return {}
