"""检索重排（SOW §3.3.2 Rerank，Top-K=5）。

- ``MetadataReranker``：**默认，零依赖**。在向量相似度基础上，按知识分类权重
  （内部规范 / 案例 > 外部趋势）、行业/标签与查询词的词面重合度微调排序，
  解决"客户评估应优先取同行业规范与案例"的精度问题。
- ``BGEReranker``：本地 CrossEncoder（BAAI/bge-reranker-v2-m3），需 ``FlagEmbedding``；
  未安装时直接抛错，由工厂回退到 MetadataReranker。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol, Sequence

from config import RAG_CATEGORY_WEIGHTS, RERANKER

logger = logging.getLogger(__name__)


@dataclass
class RerankCandidate:
    id: str
    content: str
    metadata: dict
    base_score: float


@dataclass
class RerankResult:
    id: str
    content: str
    metadata: dict
    score: float


def _tokens(text: str) -> set[str]:
    # 中文按字、英文按词，长度 >= 2 才参与词面匹配，避免单字噪声
    toks: set[str] = set()
    for m in re.findall(r"[a-zA-Z]{2,}|[\u4e00-\u9fff]{1,}", text.lower()):
        toks.add(m)
    return toks


class MetadataReranker:
    """基于相似度 + 分类权重 + 词面重合的确定性重排。"""

    def __init__(self, category_weights: dict | None = None) -> None:
        self._weights = category_weights or RAG_CATEGORY_WEIGHTS

    def rerank(
        self,
        query: str,
        candidates: Sequence[RerankCandidate],
        *,
        boost_industry: str | None = None,
    ) -> list[RerankResult]:
        q_tokens = _tokens(query)
        results: list[RerankResult] = []
        for c in candidates:
            cat = c.metadata.get("category", "")
            weight = self._weights.get(cat, 1.0)
            score = c.base_score * weight

            content_tokens = _tokens(c.content)
            overlap = len(q_tokens & content_tokens)
            if overlap:
                score *= 1 + min(overlap, 5) * 0.02  # 词面重合轻微加权

            # 同行业案例 / 指标优先
            doc_industry = c.metadata.get("industry", "")
            if boost_industry and doc_industry and doc_industry == boost_industry:
                score *= 1.1

            results.append(
                RerankResult(id=c.id, content=c.content, metadata=c.metadata, score=score)
            )
        results.sort(key=lambda r: r.score, reverse=True)
        return results


class BGEReranker:
    """本地 CrossEncoder 重排；未安装 FlagEmbedding 时抛错。"""

    def __init__(self, model_name: str) -> None:
        try:
            from FlagEmbedding import FlagReranker
        except ImportError as exc:  # pragma: no cover - 依赖可选
            raise RuntimeError("使用 BGE 重排需安装 FlagEmbedding：pip install FlagEmbedding") from exc
        self._model = FlagReranker(model_name, use_fp16=False)

    def rerank(self, query, candidates, *, boost_industry=None) -> list[RerankResult]:  # type: ignore[override]
        if not candidates:
            return []
        pairs = [(query, c.content) for c in candidates]
        scores = self._model.compute_score(pairs, normalize=True)
        results = [
            RerankResult(id=c.id, content=c.content, metadata=c.metadata, score=float(s))
            for c, s in zip(candidates, scores)
        ]
        results.sort(key=lambda r: r.score, reverse=True)
        return results


def get_reranker():  # type: ignore[no-untyped-def]
    """按配置选择重排器；bge 不可用时回退 metadata。"""
    name = (RERANKER or "metadata").lower()
    if name == "bge":
        try:
            from config import BGE_MODEL

            return BGEReranker(BGE_MODEL)
        except Exception as exc:  # pragma: no cover
            logger.warning("BGE 重排不可用，回退 metadata 重排：%s", exc)
    return MetadataReranker()
