"""T-007b rerank 客户端失败分类与边界（mock 传输，不出真实调用）。"""

import json

import httpx
import pytest

from pi_market.rerank import RerankClient, RerankError, RerankInputError


def _client(handler, retries=1):
    return RerankClient(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        timeout=0.05,
        retries=retries,
    )


def _ok(pairs, usage=None):
    return httpx.Response(
        200,
        json={
            "output": {
                "results": [
                    {"index": i, "relevance_score": s} for i, s in pairs
                ]
            },
            "usage": usage or {"total_tokens": 10},
        },
    )


def test_rerank_success_sorted_and_usage():
    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "qwen3-rerank"
        assert body["input"]["documents"] == ["a", "b", "c"]
        return _ok([(0, 0.2), (2, 0.9), (1, 0.5)])

    result = _client(handler).rerank("q", ["a", "b", "c"])
    assert [r["index"] for r in result["results"]] == [2, 1, 0]
    assert result["usage"]["total_tokens"] == 10


def test_rerank_index_out_of_range():
    with pytest.raises(RerankError) as exc:
        _client(lambda req: _ok([(0, 0.5), (3, 0.9)])).rerank("q", ["a", "b"])
    assert exc.value.category == "index"


def test_rerank_duplicate_index():
    with pytest.raises(RerankError) as exc:
        _client(lambda req: _ok([(0, 0.5), (0, 0.9)])).rerank("q", ["a", "b"])
    assert exc.value.category == "index"


def test_rerank_non_finite_score():
    body = (
        b'{"output": {"results": ['
        b'{"index": 0, "relevance_score": NaN}, {"index": 1, "relevance_score": 0.5}'
        b']}, "usage": {"total_tokens": 3}}'
    )

    def handler(request):
        return httpx.Response(200, content=body, headers={"Content-Type": "application/json"})

    with pytest.raises(RerankError) as exc:
        _client(handler).rerank("q", ["a", "b"])
    assert exc.value.category == "score"


def test_rerank_missing_results():
    with pytest.raises(RerankError) as exc:
        _client(lambda req: _ok([(0, 0.5)])).rerank("q", ["a", "b"])
    assert exc.value.category == "missing"


def test_rerank_retry_on_rate_limit_then_success():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return _ok([(0, 0.7), (1, 0.3)])

    client = _client(handler, retries=1)
    result = client.rerank("q", ["a", "b"])
    assert calls["n"] == 2
    assert client.retry_count == 1
    assert result["results"][0]["index"] == 0


def test_rerank_timeout_retries_then_raises():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.TimeoutException("timeout")

    with pytest.raises(RerankError) as exc:
        _client(handler, retries=2).rerank("q", ["a"])
    assert exc.value.category == "timeout"
    assert calls["n"] == 3


def test_rerank_abnormal_response():
    with pytest.raises(RerankError) as exc:
        _client(lambda req: httpx.Response(400, text="bad request")).rerank("q", ["a"])
    assert exc.value.category == "response"


def test_rerank_input_too_long_fails_before_http():
    def handler(request):
        raise AssertionError("不应发起 HTTP 调用")

    with pytest.raises(RerankInputError):
        _client(handler).rerank("q", ["x" * 4001])


def test_rerank_empty_documents_no_call():
    def handler(request):
        raise AssertionError("空池不应调用")

    result = _client(handler).rerank("q", [])
    assert result == {"results": [], "usage": None}
