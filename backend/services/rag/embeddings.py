"""Embedding 函数：包装大模型兼容协议 embedding 适配器。

向量库（Chroma / 内存）只接收外部算好的向量，不直接持有 embedding 模型，
因此换供应商只需改 ``.env``，向量库代码无需改动。
"""

from __future__ import annotations

import inspect
from typing import Callable, List, Sequence

from services.ai.llm_adapter import LLMUnavailableError, get_embedding_adapter
from services.ai.guardrails import sanitize_for_outbound
from config import EMBEDDING_BATCH_SIZE, EMBEDDING_DIM


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
            items = [sanitize_for_outbound(text)[0] for text in texts]
            for i in range(0, len(items), bs):
                batch = items[i : i + bs]
                try:
                    signature = inspect.signature(adapter.embed)
                    supports_dimensions = (
                        "dimensions" in signature.parameters
                        or any(
                            parameter.kind == inspect.Parameter.VAR_KEYWORD
                            for parameter in signature.parameters.values()
                        )
                    )
                except (TypeError, ValueError):
                    supports_dimensions = True
                if supports_dimensions:
                    vectors = adapter.embed(batch, dimensions=EMBEDDING_DIM)
                else:
                    vectors = adapter.embed(batch)
                if len(vectors) != len(batch):
                    raise EmbeddingUnavailableError(
                        f"Embedding 返回 {len(vectors)} 条向量，预期 {len(batch)} 条"
                    )
                invalid_dimensions = sorted(
                    {len(vector) for vector in vectors if len(vector) != EMBEDDING_DIM}
                )
                if invalid_dimensions:
                    raise EmbeddingUnavailableError(
                        "Embedding 返回维度与配置不一致："
                        f"收到 {invalid_dimensions}，预期 {EMBEDDING_DIM}；"
                        "请调整 EMBEDDING_DIM 或使用支持 dimensions 参数的模型"
                    )
                out.extend(vectors)
            return out
        except LLMUnavailableError as exc:
            raise EmbeddingUnavailableError(str(exc)) from exc

    return _embed
