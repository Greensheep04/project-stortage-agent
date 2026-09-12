"""T-004b：文本质量工具（句级切分、精确/包含/近义去重）。"""

from __future__ import annotations

import re

_SENT_SPLIT = re.compile(r"(?<=[。；！？])")


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]


def _bigrams(text: str) -> set[str]:
    s = "".join(ch for ch in text if not ch.isspace())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _similar(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def sentence_dedupe(text: str, sim_threshold: float = 0.5) -> str:
    """按句切分，去掉完全重复、包含关系与近义（bigram 相似度 ≥ 阈值）的重复句。"""
    kept: list[str] = []
    kept_cores: list[str] = []
    kept_grams: list[set[str]] = []
    for part in _SENT_SPLIT.split(text):
        s = part.strip()
        if not s:
            continue
        core = s.rstrip("。；！？")
        if any(core and k and (core in k or k in core) for k in kept_cores):
            continue
        grams = _bigrams(core)
        if any(_similar(grams, g) >= sim_threshold for g in kept_grams):
            continue
        kept.append(s)
        kept_cores.append(core)
        kept_grams.append(grams)
    return "".join(kept)


def has_intra_duplicate(text: str, sim_threshold: float = 0.5) -> bool:
    seen_cores: list[str] = []
    seen_grams: list[set[str]] = []
    for part in sentences(text):
        core = part.rstrip("。；！？")
        if any(core and k and (core in k or k in core) for k in seen_cores):
            return True
        grams = _bigrams(core)
        if any(_similar(grams, g) >= sim_threshold for g in seen_grams):
            return True
        seen_cores.append(core)
        seen_grams.append(grams)
    return False
