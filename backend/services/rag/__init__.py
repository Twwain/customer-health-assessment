"""知识库 RAG 服务（SOW §3.3）。

管道：解析 → 中文切片 → 向量化（智谱 GLM embedding-3）→ 向量库（Chroma / 内存）→ 重排 → 检索。
对外暴露 ``KnowledgeBaseService``（CRUD + 索引编排）与 ``retrieve_knowledge``（检索入口）。
"""

from .chunker import chunk_text
from .embeddings import EmbeddingUnavailableError, make_embedding_func
from .knowledge_base import KnowledgeBaseService, KnowledgeIndexError
from .parser import ParseError, ParsedDocument, parse_document
from .reranker import (
    BGEReranker,
    MetadataReranker,
    RerankCandidate,
    RerankResult,
    get_reranker,
)
from .retriever import KnowledgeRetriever, RetrievedChunk, retrieve_knowledge
from .vector_store import (
    ChromaVectorStore,
    InMemoryVectorStore,
    VectorStore,
    get_vector_store,
)

__all__ = [
    "chunk_text",
    "EmbeddingUnavailableError",
    "make_embedding_func",
    "KnowledgeBaseService",
    "KnowledgeIndexError",
    "ParseError",
    "ParsedDocument",
    "parse_document",
    "BGEReranker",
    "MetadataReranker",
    "RerankCandidate",
    "RerankResult",
    "get_reranker",
    "KnowledgeRetriever",
    "RetrievedChunk",
    "retrieve_knowledge",
    "ChromaVectorStore",
    "InMemoryVectorStore",
    "VectorStore",
    "get_vector_store",
]
