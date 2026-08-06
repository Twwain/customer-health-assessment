"""Embedding 函数：包装大模型兼容协议 embedding 适配器。

向量库（Chroma / 内存）只接收外部算好的向量，不直接持有 embedding 模型，
因此换供应商只需改 ``.env``，向量库代码无需改动。
"""

from __future__ import annotations

from typing import Callable, List, Sequence

from services.ai.llm_adapter import LLMUnavailableError, get_embedding_adapter
from config import EMBEDDING_BATCH_SIZE


class EmbeddingUnavailableError(Exception):
    """Embedding 适配器不可用（缺 Key / 无网络）。"""


def make_embedding_func(
    batch_size: int | None = None,
) -> Callable[[List[str]], List[List[float]]]:
    """返回一个 ``texts -> vectors`` 的可调用对象，按 batch 调用适配器。"""
    adapter = get_embedding_adapter()
    bs = batch_size or EMBEDDING_BATCH_SIZE

    def _embed(texts: Sequence[str]) -> List[List[float]]:
        if not texts:
            return []
        try:
            out: List[List[float]] = []
            items = list(texts)
            for i in range(0, len(items), bs):
                out.extend(adapter.embed(items[i : i + bs]))
            return out
        except LLMUnavailableError as exc:
            raise EmbeddingUnavailableError(str(exc)) from exc

    return _embed
