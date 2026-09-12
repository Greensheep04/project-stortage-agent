"""T-006a 真实向量索引：调用 DashScope text-embedding-v4 并记录 usage 与耗时。

用法（默认连 .env 的 PGDATABASE）：
    PYTHONPATH=src python3 scripts/t006/index_corpus.py t004-full-v2
"""

import json
import os
import sys
import time

sys.path.insert(0, "src")

from dotenv import load_dotenv
from openai import OpenAI

from pi_market import config, corpus


def main() -> int:
    corpus_id = sys.argv[1] if len(sys.argv) > 1 else "t004-full-v2"
    load_dotenv(str(config._ENV_PATH), override=False)
    model = os.getenv("EMBEDDING_MODEL", "text-embedding-v4")
    client = OpenAI(
        base_url=os.getenv("DASHSCOPE_BASE_URL"),
        api_key=os.getenv("DASHSCOPE_API_KEY"),
    )

    usage = {"calls": 0, "prompt_tokens": 0, "total_tokens": 0}

    def embed(text: str):
        resp = client.embeddings.create(input=[text], model=model)
        if resp.usage is not None:
            usage["calls"] += 1
            usage["prompt_tokens"] += resp.usage.prompt_tokens or 0
            usage["total_tokens"] += resp.usage.total_tokens or 0
        return resp.data[0].embedding

    started = time.time()
    result = corpus.index_corpus(corpus_id, model, embed_fn=embed)
    result["elapsed_sec"] = round(time.time() - started, 1)
    result["usage"] = usage
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
