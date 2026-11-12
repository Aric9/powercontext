"""本地确定性哈希 embedding：分词 + 字符 n-gram + 带符号特征哈希，512 维，L2 归一化。"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import Counter

from readdesk.config import EMBEDDING_DIM

try:  # jieba 可选：缺失时退化为字符 bigram
    import jieba

    jieba.setLogLevel(logging.ERROR)
    _JIEBA = jieba
except ImportError:  # pragma: no cover
    _JIEBA = None

_ASCII_RE = re.compile(r"[a-z0-9][a-z0-9._+/-]*")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_CJK = "\u4e00-\u9fff"


def tokenize(text: str) -> list[str]:
    """中英文混合分词：英文标识符/数字、中文 jieba 词、单字与相邻二元组。"""
    low = text.lower()
    tokens: list[str] = []
    for m in _ASCII_RE.finditer(low):
        tok = m.group(0).strip("._+/-")
        if len(tok) >= 2 or tok.isdigit():
            tokens.append(tok)
    for run in _CJK_RUN_RE.findall(low):
        if _JIEBA is not None:
            for w in _JIEBA.lcut(run):
                w = w.strip()
                if len(w) >= 2 and re.fullmatch(rf"[{_CJK}]+", w):
                    tokens.append(w)
        for ch in run:
            tokens.append(ch)
        for i in range(len(run) - 1):
            tokens.append(run[i : i + 2])
    return tokens


def extract_query_terms(query: str, max_terms: int = 8) -> list[str]:
    """提取检索词：英文标识符、数字、连字符术语与中文 2–4 字词，按出现顺序去重。"""
    low = query.lower()
    terms: list[str] = []

    def push(t: str) -> None:
        if t and t not in terms:
            terms.append(t)

    for m in _ASCII_RE.finditer(low):
        tok = m.group(0).strip("._+/-")
        if tok:
            push(tok)
    for run in _CJK_RUN_RE.findall(low):
        words: list[str] = []
        if _JIEBA is not None:
            words = [w for w in _JIEBA.lcut(run) if 2 <= len(w) <= 4]
        if not words:
            words = [run[i : i + 2] for i in range(len(run) - 1)] or [run]
        for w in words:
            push(w)
    return terms[:max_terms]


def embed(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """对文本生成 dim 维哈希 embedding；同文本输出恒等（确定性、可复现）。"""
    counts = Counter(tokenize(text))
    vec = [0.0] * dim
    for tok, tf in counts.items():
        digest = hashlib.blake2b(tok.encode("utf-8"), digest_size=8, person=b"readdsk").digest()
        h = int.from_bytes(digest, "little")
        idx = h % dim
        sign = 1.0 if (h >> 63) & 1 == 0 else -1.0
        vec[idx] += sign * (1.0 + math.log(tf))
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec
