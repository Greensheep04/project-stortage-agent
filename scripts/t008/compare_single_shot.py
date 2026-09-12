"""T-008b：3 个固定流程对照（现有单次 investigate vs Agent 会话）。

每个问题真实运行一次单次流程与一次 Agent 会话；记录来源正确性、调用数、耗时与
usage，供中层判断适合追问与不值得使用 Agent 的场景。不宣称 Agent 必然更准。
用法：
    PYTHONPATH=src python3 scripts/t008/compare_single_shot.py \
        --output docs/L5-执行与验证/evidence/T-008/T-008b-comparison.json
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

from pi_market.chat import ChatSession
from pi_market.investigate import investigate

DATASET = "ds-61edff98759f"
CORPUS = "t004-full-v2"

QUESTIONS = [
    {
        "qid": "CMP-01",
        "kind": "数量事实",
        "question": "SO-1003/001 的计划与已过账数量是多少？",
        "order_id": "SO-1003",
        "line_id": "001",
    },
    {
        "qid": "CMP-02",
        "kind": "处理规定",
        "question": "扫描 SKU 与计划不符时按什么流程处理？",
        "order_id": "SO-1005",
        "line_id": "001",
    },
    {
        "qid": "CMP-03",
        "kind": "历史参考",
        "question": "类似外包装受潮的历史案例是怎么处理的？",
        "order_id": "SO-1011",
        "line_id": "001",
    },
]


def run_single(item):
    started = time.time()
    result = investigate(
        DATASET,
        item["order_id"],
        line_id=item["line_id"],
        corpus=CORPUS,
        question=item["question"],
    )
    explanation = result["explanation"]
    return {
        "extension_status": result["extension_status"],
        "explanation_status": explanation["explanation_status"],
        "history_refs": [d["doc_id"] for d in result["history_refs"]],
        "applicable_rules": [d["doc_id"] for d in result["applicable_rules"]],
        "citations": explanation.get("citations", []),
        "suggestions": explanation.get("suggestions", []),
        "open_questions": explanation.get("open_questions", []),
        "call_record": result["call_record"],
        "elapsed_sec": round(time.time() - started, 1),
    }


def run_agent(item):
    started = time.time()
    session = ChatSession(DATASET, CORPUS, order_id=item["order_id"], line_id=item["line_id"])
    result = session.run_turn(item["question"])
    return {
        "text": result["text"],
        "tool_requests": result["tool_requests"],
        "planning_requests": result["planning_requests"],
        "final_generations": result["final_generations"],
        "incomplete": result["incomplete"],
        "trace": result["trace"],
        "model_usage": result.get("model_usage"),
        "generation_usage": result.get("generation_usage"),
        "explanation_status": (result.get("explanation") or {}).get("explanation_status"),
        "suggestions": (result.get("explanation") or {}).get("suggestions", []),
        "open_questions": (result.get("explanation") or {}).get("open_questions", []),
        "elapsed_sec": round(time.time() - started, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--max", type=int, default=None)
    args = parser.parse_args()

    items = QUESTIONS[: args.max] if args.max else QUESTIONS
    report = {"dataset_id": DATASET, "corpus_id": CORPUS, "items": [], "note": "允许 Agent 更慢，不宣称必然更准；适合/不适合场景由中层复核"}
    for item in items:
        single = run_single(item)
        agent = run_agent(item)
        report["items"].append({"question": item, "single_investigate": single, "agent_chat": agent})
        print(
            f"{item['qid']}: single {single['elapsed_sec']}s/{single['explanation_status']} | "
            f"agent {agent['elapsed_sec']}s tools={agent['tool_requests']} final={agent['final_generations']}",
            file=sys.stderr,
        )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"items": len(items), "output": str(output_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
