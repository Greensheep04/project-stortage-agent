"""T-007b qwen3-rerank 客户端（DashScope 原生端点，L3 v0.4 D15）。

只读 DASHSCOPE_API_KEY 与原生 rerank 端点；不修改 embedding/DeepSeek 配置。
失败分类可见（超时/限流/异常响应/index 越界或重复/非有限分数/输入超限）；
超时有界重试；显式失败不静默回退 RRF。
"""

import math
import os
import time
from typing import Any, Callable, Dict, List, Optional

import httpx
from dotenv import load_dotenv

from . import config

DEFAULT_RERANK_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
)
MAX_DOC_TOKENS = 4000
MAX_REQUEST_TOKENS = 120000


class RerankError(Exception):
    """Rerank failure with a visible category (never silently fall back)."""

    def __init__(self, message: str, category: str = "error", detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.category = category
        self.detail = detail or {}


class RerankInputError(RerankError):
    pass


def estimate_tokens(text: str) -> int:
    """Conservative upper bound without a tokenizer: 1 char ≈ 1 token for CJK."""
    return len(text)


class RerankClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 10.0,
        retries: int = 2,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        load_dotenv(str(config._ENV_PATH), override=False)
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY")
        self.base_url = base_url or os.getenv("RERANK_BASE_URL", DEFAULT_RERANK_URL)
        self.model = model or os.getenv("RERANK_MODEL", "qwen3-rerank")
        self.timeout = timeout
        self.retries = retries
        if not self.api_key:
            raise RerankError(
                "DASHSCOPE_API_KEY 未配置（rerank 走 DashScope 原生端点，不回退 embedding 配置）",
                "config",
            )
        self._client = httpx.Client(transport=transport, timeout=timeout)
        self.last_usage: Optional[Dict[str, Any]] = None
        self.call_count = 0
        self.retry_count = 0

    def rerank(
        self,
        query: str,
        documents: List[str],
        top_n: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Rerank documents for a query; returns results sorted by score desc."""
        if not query or not query.strip():
            raise RerankInputError("rerank 查询不能为空", "input")
        if not documents:
            return {"results": [], "usage": None}

        total = estimate_tokens(query)
        for i, doc in enumerate(documents):
            tokens = estimate_tokens(doc)
            if tokens > MAX_DOC_TOKENS:
                raise RerankInputError(
                    f"rerank 文档 {i} 超长: 约 {tokens} tokens > {MAX_DOC_TOKENS}",
                    "input",
                    {"index": i, "tokens": tokens},
                )
            total += tokens
        if total > MAX_REQUEST_TOKENS:
            raise RerankInputError(
                f"rerank 请求超长: 约 {total} tokens > {MAX_REQUEST_TOKENS}",
                "input",
                {"tokens": total},
            )

        payload: Dict[str, Any] = {
            "model": self.model,
            "input": {"query": query, "documents": list(documents)},
            "parameters": {"return_documents": False},
        }
        if top_n is not None:
            payload["parameters"]["top_n"] = top_n
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_error: Optional[RerankError] = None
        for attempt in range(self.retries + 1):
            try:
                response = self._client.post(self.base_url, headers=headers, json=payload)
            except httpx.TimeoutException as e:
                last_error = RerankError(f"rerank 超时: {e}", "timeout")
            except httpx.HTTPError as e:
                last_error = RerankError(f"rerank 请求失败: {e}", "transport")
            else:
                if response.status_code == 429:
                    last_error = RerankError("rerank 限流（429）", "rate_limit")
                elif response.status_code >= 500:
                    last_error = RerankError(
                        f"rerank 服务错误 {response.status_code}", "server"
                    )
                elif response.status_code != 200:
                    raise RerankError(
                        f"rerank 异常响应 {response.status_code}: {response.text[:200]}",
                        "response",
                        {"status_code": response.status_code},
                    )
                else:
                    expected = (
                        len(documents)
                        if top_n is None
                        else min(top_n, len(documents))
                    )
                    return self._parse(response, len(documents), expected)

            if attempt < self.retries:
                self.retry_count += 1
                time.sleep(min(2**attempt, 4))
        raise last_error or RerankError("rerank 未知失败", "error")

    def _parse(
        self, response: httpx.Response, document_count: int, expected: int
    ) -> Dict[str, Any]:
        try:
            body = response.json()
        except Exception as e:
            raise RerankError(f"rerank 响应无法解析: {e}", "response") from e
        results = (body.get("output") or {}).get("results")
        if not isinstance(results, list):
            raise RerankError("rerank 响应缺少 output.results", "response")

        seen = set()
        parsed = []
        for item in results:
            index = item.get("index")
            score = item.get("relevance_score")
            if not isinstance(index, int) or index < 0 or index >= document_count:
                raise RerankError(
                    f"rerank index 越界: {index}（候选 {document_count} 篇）",
                    "index",
                    {"index": index},
                )
            if index in seen:
                raise RerankError(f"rerank index 重复: {index}", "index", {"index": index})
            seen.add(index)
            if not isinstance(score, (int, float)) or not math.isfinite(score):
                raise RerankError(f"rerank 分数非有限: {score!r}", "score", {"index": index})
            parsed.append({"index": index, "relevance_score": float(score)})

        if len(parsed) != expected:
            raise RerankError(
                f"rerank 返回结果缺失: {len(parsed)}/{expected}",
                "missing",
                {"returned": len(parsed), "expected": expected},
            )
        parsed.sort(key=lambda x: (-x["relevance_score"], x["index"]))
        self.last_usage = body.get("usage")
        self.call_count += 1
        return {"results": parsed, "usage": self.last_usage}


def get_rerank_client(model: Optional[str] = None) -> RerankClient:
    return RerankClient(model=model)
