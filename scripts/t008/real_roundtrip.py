"""T-008a 最小真实工具往返：真实 DeepSeek tool_calls + 真实只读业务数据。

用法（默认连 .env 的 PGDATABASE）：
    PYTHONPATH=src python3 scripts/t008/real_roundtrip.py [问题] [订单] [明细]
"""

import json
import sys

sys.path.insert(0, "src")

from pi_market.chat import ChatSession

DATASET = "ds-61edff98759f"
CORPUS = "t004-full-v2"


def main() -> int:
    argv = sys.argv[1:]
    unbound = bool(argv) and argv[0] == "--unbound"
    if unbound:
        argv = argv[1:]
        question = argv[0] if argv else "帮我找提到待检的现场事件，定位相关订单"
        session = ChatSession(DATASET, CORPUS)
    else:
        question = argv[0] if argv else "请核对该订单的计划与已过账数量，并查一下包装异常的处理规范"
        order = argv[1] if len(argv) > 1 else "SO-1003"
        line = argv[2] if len(argv) > 2 else "001"
        session = ChatSession(DATASET, CORPUS, order_id=order, line_id=line)
    print(session.startup_message, file=sys.stderr)
    result = session.run_turn(question)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result["incomplete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
