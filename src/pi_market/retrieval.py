import os
import re
from collections import defaultdict
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import psycopg
from dotenv import load_dotenv

from . import config, db
from .evidence import EvidenceError
from .importer import _parse_as_of


class RetrievalError(Exception):
    pass


class IndexNotReadyError(RetrievalError):
    """Raised when the requested index state does not allow the chosen mode."""

    def __init__(self, message: str, detail: Dict[str, Any]):
        super().__init__(message)
        self.detail = detail


_CJK_PUNCT = "，。、；：？！""''（）【】《》“”‘’—…"
_PUNCT_PATTERN = re.compile(r"[\s" + re.escape(_CJK_PUNCT + r"""!"#$%&'()*+,-./:;<=>?@[\\\]^_`{|}~""") + r"]+")


def normalize_text(text: str) -> str:
    """Remove punctuation and whitespace for phrase matching."""
    return _PUNCT_PATTERN.sub("", text).lower()


def _bigrams(text: str) -> set[str]:
    s = normalize_text(text)
    if len(s) < 2:
        return set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def phrase_score(query: str, content: str) -> float:
    """Dice coefficient over character bigrams."""
    q = _bigrams(query)
    c = _bigrams(content)
    if not q or not c:
        return 0.0
    inter = len(q & c)
    return 2.0 * inter / (len(q) + len(c))


def _get_dataset_as_of(cur: psycopg.Cursor, dataset_id: str) -> datetime:
    cur.execute(
        "SELECT as_of, status FROM dataset WHERE dataset_id = %s",
        (dataset_id,),
    )
    row = cur.fetchone()
    if not row:
        raise RetrievalError(f"数据集不存在: {dataset_id}")
    as_of, status = row
    if status != "ready":
        raise RetrievalError(f"数据集未就绪: {dataset_id} 状态={status}")
    return as_of


def _resolve_as_of(dataset_id: str, as_of: Optional[Any]) -> datetime:
    if as_of is None:
        with db.get_reader_conn() as conn:
            with conn.cursor() as cur:
                return _get_dataset_as_of(cur, dataset_id)
    return _parse_as_of(as_of)


def _row_to_chunk(row: Tuple[Any, ...], cols: List[str]) -> Dict[str, Any]:
    return dict(zip(cols, row))


def _check_index_readiness(
    cur: psycopg.Cursor,
    dataset_id: str,
    embedding_model: str,
    require_vectors: bool,
) -> None:
    cur.execute(
        """
        SELECT COUNT(*), COUNT(embedding)
        FROM chunk
        WHERE dataset_id = %s AND embedding_model = %s
        """,
        (dataset_id, embedding_model),
    )
    total, vector_ready = cur.fetchone()
    total = total or 0
    vector_ready = vector_ready or 0

    detail = {
        "dataset_id": dataset_id,
        "embedding_model": embedding_model,
        "chunks": total,
        "vector_ready": vector_ready,
    }

    if total == 0:
        raise IndexNotReadyError("索引未就绪：该数据集尚未建立 chunk 索引", detail)

    if require_vectors and vector_ready < total:
        raise IndexNotReadyError(
            "索引未就绪：向量嵌入尚未全部填充完成",
            detail,
        )


def _list_chunks_for_phrase(
    cur: psycopg.Cursor,
    dataset_id: str,
    embedding_model: str,
    as_of: datetime,
) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT
            chunk_id, parent_type, parent_id, order_id, line_id, tx_id,
            record_time, content, source_sheet, source_row
        FROM chunk
        WHERE dataset_id = %s AND embedding_model = %s
          AND (record_time IS NULL OR record_time <= %s)
        ORDER BY source_row
        """,
        (dataset_id, embedding_model, as_of),
    )
    cols = [desc[0] for desc in cur.description]
    return [_row_to_chunk(row, cols) for row in cur.fetchall()]


def _search_phrase(
    cur: psycopg.Cursor,
    dataset_id: str,
    embedding_model: str,
    as_of: datetime,
    query: str,
    k: int,
) -> List[Dict[str, Any]]:
    chunks = _list_chunks_for_phrase(cur, dataset_id, embedding_model, as_of)
    for chunk in chunks:
        chunk["score"] = phrase_score(query, chunk["content"])
    # Zero-score chunks have no phrase hit: exclude them from phrase recall and
    # from the RRF candidate pool. Stable secondary order by chunk_id.
    hits = [c for c in chunks if c["score"] > 0]
    hits.sort(key=lambda c: (-c["score"], c["chunk_id"]))
    return hits[:k]


def _search_vector(
    cur: psycopg.Cursor,
    dataset_id: str,
    embedding_model: str,
    as_of: datetime,
    query: str,
    k: int,
    embed_fn: Callable[[str], List[float]],
) -> List[Dict[str, Any]]:
    qvec = embed_fn(query)
    if len(qvec) != 1024:
        raise RetrievalError(f"查询向量维度错误: 期望 1024, 实际 {len(qvec)}")

    cur.execute(
        """
        SELECT
            chunk_id, parent_type, parent_id, order_id, line_id, tx_id,
            record_time, content, source_sheet, source_row,
            embedding <=> %s::vector AS distance
        FROM chunk
        WHERE dataset_id = %s AND embedding_model = %s
          AND embedding IS NOT NULL
          AND (record_time IS NULL OR record_time <= %s)
        ORDER BY distance, chunk_id
        LIMIT %s
        """,
        (qvec, dataset_id, embedding_model, as_of, k),
    )
    cols = [desc[0] for desc in cur.description]
    results = []
    for row in cur.fetchall():
        chunk = _row_to_chunk(row, cols)
        chunk["score"] = 1.0 - chunk["distance"]
        results.append(chunk)
    return results


def _search_rrf(
    cur: psycopg.Cursor,
    dataset_id: str,
    embedding_model: str,
    as_of: datetime,
    query: str,
    k: int,
    embed_fn: Callable[[str], List[float]],
    rrf_k: int = 60,
    candidate_factor: int = 10,
) -> List[Dict[str, Any]]:
    # Candidate pool size: ensure enough parents for meaningful fusion.
    pool_size = max(k * candidate_factor, 100)

    phrase_results = _search_phrase(cur, dataset_id, embedding_model, as_of, query, pool_size)
    vector_results = _search_vector(cur, dataset_id, embedding_model, as_of, query, pool_size, embed_fn)

    chunk_scores: Dict[str, float] = defaultdict(float)
    chunk_info: Dict[str, Dict[str, Any]] = {}

    for rank, chunk in enumerate(phrase_results, start=1):
        cid = chunk["chunk_id"]
        chunk_scores[cid] += 1.0 / (rank + rrf_k)
        chunk_info[cid] = chunk

    for rank, chunk in enumerate(vector_results, start=1):
        cid = chunk["chunk_id"]
        chunk_scores[cid] += 1.0 / (rank + rrf_k)
        chunk_info.setdefault(cid, chunk)

    sorted_chunks = sorted(chunk_scores.items(), key=lambda x: (-x[1], x[0]))
    return [
        {**chunk_info[cid], "rrf_score": score}
        for cid, score in sorted_chunks[:k]
    ]


def get_embedder(
    model: Optional[str] = None,
) -> Tuple[Callable[[str], List[float]], str]:
    """Return a real embedding callable and the model name from environment config.

    The returned callable is swappable in tests by passing embed_fn directly.
    """
    from dotenv import load_dotenv
    from openai import OpenAI

    load_dotenv(str(config._ENV_PATH), override=False)

    api_key = os.getenv("DASHSCOPE_API_KEY")
    base_url = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    model = model or os.getenv("EMBEDDING_MODEL", "text-embedding-v4")

    if not api_key:
        raise RetrievalError("环境变量 DASHSCOPE_API_KEY 未配置")

    client = OpenAI(base_url=base_url, api_key=api_key)

    def embed(text: str) -> List[float]:
        resp = client.embeddings.create(input=[text], model=model)
        return resp.data[0].embedding

    return embed, model


def search(
    dataset_id: str,
    query: str,
    mode: str = "rrf",
    k: int = 10,
    as_of: Optional[Any] = None,
    embed_fn: Optional[Callable[[str], List[float]]] = None,
    embedding_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Search evidence chunks in phrase, vector, or rrf mode.

    All modes respect dataset_id and record_time <= as_of.
    Results are returned as relevant samples, not a complete enumeration.
    """
    if not query or not query.strip():
        raise RetrievalError("查询描述不能为空")
    if mode not in {"phrase", "vector", "rrf"}:
        raise RetrievalError(f"不支持的检索模式: {mode}")

    effective_as_of = _resolve_as_of(dataset_id, as_of)

    real_embed_fn = embed_fn
    if embedding_model is None:
        if mode == "phrase":
            load_dotenv(str(config._ENV_PATH), override=False)
            embedding_model = os.getenv("EMBEDDING_MODEL", "text-embedding-v4")
        elif embed_fn is not None:
            raise RetrievalError("显式提供 embed_fn 时也必须提供 embedding_model")
        else:
            real_embed_fn, embedding_model = get_embedder(embedding_model)

    if mode in {"vector", "rrf"} and real_embed_fn is None:
        real_embed_fn, detected_model = get_embedder(embedding_model)
        if embedding_model is None:
            embedding_model = detected_model

    with db.get_reader_conn() as conn:
        with conn.cursor() as cur:
            _check_index_readiness(
                cur, dataset_id, embedding_model, require_vectors=(mode in {"vector", "rrf"})
            )
            if mode == "phrase":
                results = _search_phrase(
                    cur, dataset_id, embedding_model, effective_as_of, query, k
                )
            elif mode == "vector":
                results = _search_vector(
                    cur, dataset_id, embedding_model, effective_as_of, query, k, real_embed_fn
                )
            else:
                results = _search_rrf(
                    cur, dataset_id, embedding_model, effective_as_of, query, k, real_embed_fn
                )

    return {
        "dataset_id": dataset_id,
        "query": query,
        "mode": mode,
        "k": k,
        "as_of": effective_as_of.isoformat(),
        "embedding_model": embedding_model,
        "disclaimer": "Top-k 结果为相关样本，不代表完整统计。",
        "results": [
            {
                "rank": i + 1,
                "chunk_id": r["chunk_id"],
                "parent_type": r["parent_type"],
                "parent_id": r["parent_id"],
                "order_id": r["order_id"],
                "line_id": r["line_id"],
                "tx_id": r["tx_id"],
                "record_time": r["record_time"].isoformat() if r["record_time"] else None,
                "content": r["content"],
                "source_sheet": r["source_sheet"],
                "source_row": r["source_row"],
                "score": r.get("score"),
                "distance": r.get("distance"),
                "rrf_score": r.get("rrf_score"),
            }
            for i, r in enumerate(results)
        ],
    }
