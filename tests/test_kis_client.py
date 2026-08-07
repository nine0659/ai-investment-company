"""get_fluctuation_rank TR_ID/파라미터 회귀 테스트.

기존 코드는 TR_ID "FHPST01760000"(시간외잔량순위 API 소속)을 "/ranking/fluctuation"
엔드포인트에 잘못 보내고 있었다 — KIS가 매 요청을 거부했지만 _rank()의
except Exception: return [] 에 삼켜져 "데이터 없음"으로 오인됐다.
2026-08-07 /rebound 장중 실전 실행에서도 재현 확인 후 수정.
"""
import clients.kis_client as kis_client


class _FakeResponse:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"output": []}


def test_get_fluctuation_rank_uses_correct_tr_id_and_sort_field(monkeypatch):
    monkeypatch.setattr(kis_client, "_rank_api_available", lambda: True)

    captured = {}

    def fake_get(url, headers, params, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["params"] = params
        return _FakeResponse()

    monkeypatch.setattr(kis_client.requests, "get", fake_get)

    client = kis_client.KISClient()
    monkeypatch.setattr(client, "_get_token", lambda: "dummy-token")

    client.get_fluctuation_rank(market="J", rise=False, top_n=20)

    assert captured["url"].endswith("/uapi/domestic-stock/v1/ranking/fluctuation")
    assert captured["headers"]["tr_id"] == "FHPST01700000"
    # 하락률 순위는 FID_RANK_SORT_CLS_CODE=1 (FID_DIV_CLS_CODE가 아님 — 과거 버그)
    assert captured["params"]["FID_RANK_SORT_CLS_CODE"] == "1"

    client.get_fluctuation_rank(market="J", rise=True, top_n=20)
    assert captured["params"]["FID_RANK_SORT_CLS_CODE"] == "0"
