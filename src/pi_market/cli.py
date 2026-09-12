import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import (
    corpus,
    corpus_retrieval,
    db,
    rerank,
    evaluate,
    evidence,
    explain,
    importer,
    investigate,
    reconcile,
    retrieval,
)
from .config import _ENV_PATH


def _default_embedding_model() -> str:
    load_dotenv(str(_ENV_PATH), override=False)
    return os.getenv("EMBEDDING_MODEL", "text-embedding-v4")


def _json(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def cmd_import(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        print(_json({"error": f"文件不存在: {path}"}), file=sys.stderr)
        return 1
    db.init_db()
    result = importer.import_excel(path, args.as_of)
    print(_json(result))
    return 0 if result["status"] == "ready" else 2


def cmd_reconcile(args: argparse.Namespace) -> int:
    db.init_db()
    try:
        result = reconcile.reconcile(
            args.dataset_id,
            args.order_id,
            line_id=args.line_id,
            as_of=args.as_of,
        )
        print(_json(result))
        return 0
    except reconcile.ReconcileError as e:
        print(_json({"error": str(e)}), file=sys.stderr)
        return 1


def cmd_index(args: argparse.Namespace) -> int:
    db.init_db()
    if args.no_embedding:
        try:
            result = evidence.build_chunks(args.dataset_id, args.embedding_model or _default_embedding_model())
            print(_json(result))
            return 0
        except evidence.EvidenceError as e:
            print(_json({"error": str(e)}), file=sys.stderr)
            return 1

    try:
        embed_fn, model = retrieval.get_embedder(args.embedding_model)
    except retrieval.RetrievalError as e:
        print(_json({"error": str(e)}), file=sys.stderr)
        return 1
    try:
        result = evidence.build_index(args.dataset_id, model, embed_fn)
        print(_json(result))
        if result["status"] == "ready":
            return 0
        # Any non-ready state (partial fill, embedding service failure, etc.)
        # means chunks are persisted but vectors are not fully established.
        print(_json({"error": "模型服务不可用，chunk 已就绪、向量未建立", **result}), file=sys.stderr)
        return 2
    except (evidence.EvidenceError, retrieval.RetrievalError) as e:
        print(_json({"error": str(e)}), file=sys.stderr)
        return 1
    except Exception as e:
        print(_json({"error": f"索引失败: {e}"}), file=sys.stderr)
        return 2


def cmd_import_corpus(args: argparse.Namespace) -> int:
    db.init_db()
    try:
        result = corpus.import_corpus(args.corpus_id, args.dataset_id, files_dir=args.dir)
        print(_json(result))
        return 0
    except corpus.CorpusValidationError as e:
        print(_json({"error": str(e), "issues": e.issues}), file=sys.stderr)
        return 2
    except corpus.CorpusError as e:
        print(_json({"error": str(e)}), file=sys.stderr)
        return 1


def cmd_index_corpus(args: argparse.Namespace) -> int:
    db.init_db()
    if args.no_embedding:
        try:
            result = corpus.index_corpus(
                args.corpus_id,
                args.embedding_model or _default_embedding_model(),
                no_embedding=True,
            )
            print(_json(result))
            return 0
        except corpus.CorpusError as e:
            print(_json({"error": str(e)}), file=sys.stderr)
            return 1

    try:
        embed_fn, model = retrieval.get_embedder(args.embedding_model)
    except retrieval.RetrievalError as e:
        print(_json({"error": str(e)}), file=sys.stderr)
        return 1
    try:
        result = corpus.index_corpus(args.corpus_id, model, embed_fn=embed_fn)
        print(_json(result))
        if result["status"] == "ready":
            return 0
        print(
            _json({"error": "模型服务不可用，chunk 已就绪、向量未建立", **result}),
            file=sys.stderr,
        )
        return 2
    except (corpus.CorpusError, retrieval.RetrievalError) as e:
        print(_json({"error": str(e)}), file=sys.stderr)
        return 1
    except Exception as e:
        print(_json({"error": f"索引失败: {e}"}), file=sys.stderr)
        return 2


def cmd_search(args: argparse.Namespace) -> int:
    db.init_db()
    if args.type and not args.corpus:
        print(_json({"error": "--type 仅在 --corpus 下有效"}), file=sys.stderr)
        return 1
    if args.as_of and not args.corpus:
        print(_json({"error": "--as-of 仅在 --corpus 下有效"}), file=sys.stderr)
        return 1
    if args.ranker and not args.corpus:
        print(_json({"error": "--ranker 仅在 --corpus 下有效"}), file=sys.stderr)
        return 1
    if args.corpus:
        try:
            kwargs = {}
            if args.embedding_model is not None:
                kwargs["embedding_model"] = args.embedding_model
            if args.as_of is not None:
                kwargs["as_of"] = args.as_of
            if args.ranker:
                kwargs["scheme"] = args.ranker
                if args.ranker == "rrf+rerank":
                    from .rerank import get_rerank_client

                    kwargs["rerank_fn"] = get_rerank_client().rerank
            result = corpus_retrieval.search_corpus(
                args.dataset_id,
                args.corpus,
                args.query,
                doc_types=args.type,
                mode=args.mode,
                k=args.k,
                **kwargs,
            )
            print(_json(result))
            return 0
        except corpus_retrieval.CorpusIndexNotReadyError as e:
            print(_json({"error": str(e), "index_status": e.detail}), file=sys.stderr)
            return 2
        except rerank.RerankError as e:
            print(_json({"error": str(e), "rerank_category": e.category}), file=sys.stderr)
            return 1
        except (corpus.CorpusError, retrieval.RetrievalError) as e:
            print(_json({"error": str(e)}), file=sys.stderr)
            return 1

    try:
        kwargs = {}
        if args.embedding_model is not None:
            kwargs["embedding_model"] = args.embedding_model
        result = retrieval.search(
            args.dataset_id,
            args.query,
            mode=args.mode,
            k=args.k,
            **kwargs,
        )
        print(_json(result))
        return 0
    except retrieval.IndexNotReadyError as e:
        print(_json({"error": str(e), "index_status": e.detail}), file=sys.stderr)
        return 2
    except retrieval.RetrievalError as e:
        print(_json({"error": str(e)}), file=sys.stderr)
        return 1


def cmd_investigate(args: argparse.Namespace) -> int:
    db.init_db()
    try:
        inv = investigate.investigate(
            args.dataset_id,
            args.order_id,
            line_id=args.line_id,
            as_of=args.as_of,
            corpus=args.corpus,
            question=args.question,
            scheme=args.ranker,
        )
        if args.corpus:
            exp = inv["explanation"]
            output = {
                "dataset_id": inv["dataset_id"],
                "order_id": inv["order_id"],
                "line_id": inv["line_id"],
                "as_of": inv["as_of"],
                "corpus_id": inv["corpus_id"],
                "facts": inv["facts"],
                "evidence": inv["evidence"],
                "event_evidence": inv["event_evidence"],
                "history_refs": inv["history_refs"],
                "applicable_rules": inv["applicable_rules"],
                "retrieval_description": inv["retrieval_description"],
                "extension_status": inv["extension_status"],
                "extension_error": inv["extension_error"],
                "explanation": exp,
                "suggestions": inv["suggestions"],
                "call_record": inv["call_record"],
            }
            print(_json(output))
            if exp["explanation_status"] != "ok":
                return 2
            if inv["extension_status"] != "ok":
                return 2
            return 0

        exp = explain.explain(inv["facts"], inv["evidence"])
        output = {
            "dataset_id": inv["dataset_id"],
            "order_id": inv["order_id"],
            "line_id": inv["line_id"],
            "as_of": inv["as_of"],
            "facts": inv["facts"],
            "evidence": inv["evidence"],
            "explanation": exp,
        }
        print(_json(output))
        return 0 if exp["explanation_status"] == "ok" else 2
    except investigate.InvestigateError as e:
        print(_json({"error": str(e)}), file=sys.stderr)
        return 1
    except Exception as e:
        print(_json({"error": f"调查失败: {e}"}), file=sys.stderr)
        return 2


def cmd_chat(args: argparse.Namespace) -> int:
    db.init_db()
    from .chat import HELP_TEXT, ChatSession

    try:
        session = ChatSession(
            args.dataset_id,
            args.corpus_id,
            order_id=args.order,
            line_id=args.line_id,
            as_of=args.as_of,
            transport=args.transport,
        )
    except Exception as e:
        print(_json({"error": f"会话启动失败: {e}"}), file=sys.stderr)
        return 2

    if session.startup_message:
        print(session.startup_message)
    print(
        f"范围：dataset={args.dataset_id} corpus={args.corpus_id}；"
        f"工具传输：{args.transport}；实际截止时间 {session.current_as_of()}"
    )
    print(HELP_TEXT)
    try:
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("/"):
                if line.split()[0] == "/exit":
                    break
                reply = session.handle_command(line)
                if reply:
                    print(reply)
                continue
            try:
                result = session.run_turn(line)
            except Exception as e:
                print(_json({"error": f"本轮失败: {e}"}), file=sys.stderr)
                continue
            print(result["text"])
            if result.get("incomplete") and "未完成" not in result["text"]:
                print("本轮调查未完成（原因见上）")
            if args.debug:
                for record in result["trace"]:
                    if record.get("tool") or record.get("op"):
                        print("  [tool] " + json.dumps(record, ensure_ascii=False))
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        session.close()
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    try:
        report = evaluate.run_evaluation(
            args.dataset_id,
            args.answers,
            args.queries,
            k=args.k,
            embedding_model=args.embedding_model,
            as_of=args.as_of,
        )
    except evaluate.EvaluateError as e:
        print(_json({"error": str(e)}), file=sys.stderr)
        return 1

    text = _json(report)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text)

    modes = report["retrieval"]["modes"]
    return 0 if all(m.get("status") == "ok" for m in modes) else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pi-market")
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="导入 Excel 业务快照")
    p_import.add_argument("file", help="Excel 文件路径")
    p_import.add_argument(
        "--as-of",
        default="2026-09-09 20:00:00",
        help="核对截止时间（默认 2026-09-09 20:00:00）",
    )
    p_import.set_defaults(func=cmd_import)

    p_reconcile = sub.add_parser("reconcile", help="精确核对订单明细")
    p_reconcile.add_argument("dataset_id", help="数据集 ID")
    p_reconcile.add_argument("order_id", help="订单号")
    p_reconcile.add_argument("--line-id", help="订单明细号（可选）")
    p_reconcile.add_argument("--as-of", help="自定义截止时间（覆盖数据集默认值）")
    p_reconcile.set_defaults(func=cmd_reconcile)

    p_index = sub.add_parser("index", help="为数据集建立检索索引")
    p_index.add_argument("dataset_id", help="数据集 ID")
    p_index.add_argument(
        "--embedding-model",
        help="指定 chunk 与向量所属模型标识（默认读取 EMBEDDING_MODEL）",
    )
    p_index.add_argument(
        "--no-embedding",
        action="store_true",
        help="仅建立 chunk，不调用 embedding 模型",
    )
    p_index.set_defaults(func=cmd_index)

    p_import_corpus = sub.add_parser("import-corpus", help="导入白名单语料版本（D9）")
    p_import_corpus.add_argument("corpus_id", help="语料版本 ID，如 t004-full-v2")
    p_import_corpus.add_argument("--dataset-id", required=True, help="绑定的数据集 ID")
    p_import_corpus.add_argument(
        "--dir",
        help="语料目录（默认 deliverables/t004/full-v2）",
    )
    p_import_corpus.set_defaults(func=cmd_import_corpus)

    p_index_corpus = sub.add_parser("index-corpus", help="为语料版本建立 section 索引")
    p_index_corpus.add_argument("corpus_id", help="语料版本 ID")
    p_index_corpus.add_argument(
        "--embedding-model",
        help="指定向量模型标识（默认读取 EMBEDDING_MODEL）",
    )
    p_index_corpus.add_argument(
        "--no-embedding",
        action="store_true",
        help="仅建立 chunk，不调用 embedding 模型",
    )
    p_index_corpus.set_defaults(func=cmd_index_corpus)

    p_search = sub.add_parser("search", help="混合检索证据")
    p_search.add_argument("dataset_id", help="数据集 ID")
    p_search.add_argument("query", help="检索描述")
    p_search.add_argument("--k", type=int, default=10)
    p_search.add_argument("--mode", default="rrf", choices=["phrase", "vector", "rrf"])
    p_search.add_argument(
        "--embedding-model",
        help="指定 chunk 所属模型标识（默认读取 EMBEDDING_MODEL）",
    )
    p_search.add_argument("--corpus", help="语料版本 ID（启用新语料过滤检索）")
    p_search.add_argument(
        "--type",
        action="append",
        choices=["event", "case", "sop"],
        help="资料类型（可重复；仅 --corpus 下有效）",
    )
    p_search.add_argument("--as-of", help="截止时间（仅 --corpus 下有效）")
    p_search.add_argument(
        "--ranker",
        default=None,
        choices=["phrase", "vector", "rrf", "rrf+rerank"],
        help="显式检索方案（仅 --corpus 下有效；与评测同口径的父文档排名；不传走旧路径）",
    )
    p_search.set_defaults(func=cmd_search)

    p_inv = sub.add_parser("investigate", help="调查并生成事实与证据包")
    p_inv.add_argument("dataset_id", help="数据集 ID")
    p_inv.add_argument("order_id", help="订单号")
    p_inv.add_argument("--line-id", help="订单明细号（可选）")
    p_inv.add_argument("--as-of", help="截止时间")
    p_inv.add_argument("--corpus", help="语料版本 ID（启用历史案例与有效 SOP 扩展调查）")
    p_inv.add_argument("--question", help="调查问题（缺省由事实构造检索描述）")
    p_inv.add_argument(
        "--ranker",
        default=None,
        choices=["phrase", "vector", "rrf", "rrf+rerank"],
        help="显式检索方案（历史/规则池；与评测同口径；不传走旧默认路径）",
    )
    p_inv.set_defaults(func=cmd_investigate)

    p_chat = sub.add_parser("chat", help="有限只读 Agent 终端会话（T-008a 骨架）")
    p_chat.add_argument("dataset_id", help="数据集 ID")
    p_chat.add_argument("corpus_id", help="语料版本 ID")
    p_chat.add_argument("--order", help="启动时绑定订单")
    p_chat.add_argument("--line-id", help="启动时绑定的明细")
    p_chat.add_argument("--as-of", help="截止时间覆盖（默认数据集快照时间并显示）")
    p_chat.add_argument(
        "--transport",
        choices=["function", "mcp"],
        default="function",
        help="工具传输：function=宿主内直接调用（默认）；mcp=本地 MCP stdio 服务",
    )
    p_chat.add_argument(
        "--debug",
        action="store_true",
        help="显示内部工具 trace（默认隐藏，只打印回答与未完成提示）",
    )
    p_chat.set_defaults(func=cmd_chat)

    p_eval = sub.add_parser("evaluate", help="独立评测：数值核对 + 检索三模式 Recall@10")
    p_eval.add_argument("dataset_id", help="数据集 ID")
    p_eval.add_argument("answers", help="答案 JSON 路径（仅评测入口读取）")
    p_eval.add_argument("queries", help="查询集 JSONL 路径")
    p_eval.add_argument("--k", type=int, default=10, help="Top-k（默认 10）")
    p_eval.add_argument("--embedding-model", help="指定 embedding 模型标识")
    p_eval.add_argument("--as-of", help="自定义截止时间（覆盖数据集默认值）")
    p_eval.add_argument("--output", help="报告写入路径（缺省打印到 stdout）")
    p_eval.set_defaults(func=cmd_evaluate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
