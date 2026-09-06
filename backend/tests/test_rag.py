"""RAG 知识库测试。

覆盖：文档解析、中文切片、向量存储（内存）、重排、检索过滤、知识库 CRUD、
上传→索引→检索→审核→删除→重索引全链路，以及知识上下文注入对话 Prompt。

Embedding 用「词袋」假函数（中文按字向量化），使检索相关性可复现且不依赖网络。
"""

import io
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import config
from database import Base, get_db
from models import (
    Customer,
    KnowledgeChunk,
    KnowledgeItem,
    KnowledgeVectorMetadataOutbox,
)
from services.rag.chunker import chunk_text
from services.rag.embeddings import EmbeddingUnavailableError, make_embedding_func
from services.rag.parser import ParseError, parse_document
from services.rag.reranker import MetadataReranker, RerankCandidate
from services.rag.retriever import RetrievedChunk, retrieve_knowledge
from services.rag.vector_store import InMemoryVectorStore, get_vector_store

METHODOLOGY_MD = """# 客户健康度评估方法论

回款状态为严重逾期时应优先安排回款催收，评估竞品介入风险。
KCR 关键客户关系由决策链覆盖度与关键人支持度决定。行业基准健康度均值约 72 分。
"""

TREND_MD = """# 外部行业趋势

今年政企市场竞争加剧，友商在政务云领域持续低价切入。
行业整体增速放缓，建议重点关注回款与续约风险。
"""


# ══════════════════════════ 夹具 ════════════════════════════════════════════


@pytest.fixture()
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    try:
        yield eng
    finally:
        Base.metadata.drop_all(bind=eng)


@pytest.fixture()
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture()
def db(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def memory_store():
    return InMemoryVectorStore()


def _bag_of_chars_embed(texts):
    """词袋假 embedding：中文按字映射到 64 维向量，使字符重叠→余弦相似。"""
    out = []
    for t in texts:
        vec = [0.0] * 64
        for ch in t:
            vec[ord(ch) % 64] += 1.0
        out.append(vec)
    return out


@pytest.fixture()
def fake_embed():
    return _bag_of_chars_embed


# ══════════════════════════ 解析 ════════════════════════════════════════════


def test_parse_markdown_and_txt():
    parsed = parse_document("m.md", raw="# 标题\n正文".encode("utf-8"))
    assert "标题" in parsed.text and "正文" in parsed.text


def test_parse_csv_roundtrip():
    csv_raw = "a,b\n1,2\n3,4\n".encode("utf-8")
    parsed = parse_document("x.csv", raw=csv_raw)
    assert "a,b" in parsed.text
    assert parsed.text.count("\n") == 2


def test_parse_unknown_type_raises():
    with pytest.raises(ParseError):
        parse_document("x.xyz", raw=b"data")


# ══════════════════════════ 切片 ════════════════════════════════════════════


def test_chunk_text_basic():
    text = "。".join([f"第{i}句内容关于客情评估指标" for i in range(20)])
    chunks = chunk_text(text, chunk_size=40, overlap=10)
    assert len(chunks) > 1
    # 每个切片不超过 chunk_size + overlap
    for c in chunks:
        assert len(c) <= 40 + 10 + 5
    # 原文句子不丢失
    for i in range(20):
        assert any(f"第{i}句" in c for c in chunks)


def test_chunk_text_empty():
    assert chunk_text("") == []
    assert chunk_text("   \n  ") == []


# ══════════════════════════ Embedding ════════════════════════════════════════


def test_embedding_unavailable_raises():
    # 测试环境无 EMBEDDING_API_KEY → 适配器不可用 → 抛 EmbeddingUnavailableError
    with pytest.raises(EmbeddingUnavailableError):
        make_embedding_func()(["任意文本"])


def test_embedding_func_requests_configured_dimensions(monkeypatch):
    import services.rag.embeddings as embeddings

    calls: list[tuple[list[str], int | None]] = []

    class StubAdapter:
        def embed(self, texts, dimensions=None):
            calls.append((list(texts), dimensions))
            return [[0.0] * int(dimensions) for _ in texts]

    monkeypatch.setattr(embeddings, "get_embedding_adapter", lambda: StubAdapter())
    monkeypatch.setattr(embeddings, "EMBEDDING_DIM", 7)
    result = embeddings.make_embedding_func()(["测试"])
    assert calls == [(["测试"], 7)]
    assert len(result[0]) == 7


def test_embedding_func_rejects_gateway_native_dimension(monkeypatch):
    import services.rag.embeddings as embeddings

    class StubAdapter:
        def embed(self, texts, dimensions=None):
            return [[0.0] * 5 for _ in texts]

    monkeypatch.setattr(embeddings, "get_embedding_adapter", lambda: StubAdapter())
    monkeypatch.setattr(embeddings, "EMBEDDING_DIM", 7)
    with pytest.raises(EmbeddingUnavailableError, match="返回维度与配置不一致"):
        embeddings.make_embedding_func()(["测试"])


# ══════════════════════════ 向量存储 ════════════════════════════════════════


def test_memory_store_add_query_delete(memory_store):
    memory_store.add(
        ["a", "b"],
        ["回款风险处置", "行业趋势分析"],
        _bag_of_chars_embed(["回款风险处置", "行业趋势分析"]),
        [{"status": "canonical", "category": "内部规范"}, {"status": "canonical", "category": "外部指标"}],
    )
    assert memory_store.count() == 2
    res = memory_store.query(_bag_of_chars_embed(["回款风险"])[0], top_k=1, where={"status": "canonical"})
    assert res[0]["id"] == "a"  # 字符重叠更高的应排前
    # 过滤：只取 external
    res2 = memory_store.query(_bag_of_chars_embed(["行业"])[0], top_k=5, where={"category": "外部指标"})
    assert all(r["metadata"]["category"] == "外部指标" for r in res2)
    # 更新 metadata
    memory_store.update_metadatas(["a"], [{"status": "canonical", "category": "外部指标"}])
    assert memory_store.query(_bag_of_chars_embed(["x"])[0], top_k=5, where={"category": "外部指标"})[0]["id"] == "a"
    # 删除
    memory_store.delete(["a"])
    assert memory_store.count() == 1


def test_get_vector_store_memory(monkeypatch):
    monkeypatch.setattr(config, "KNOWLEDGE_VECTOR_STORE", "memory")
    import services.rag.vector_store as vs

    vs._MEMORY_STORE = None
    store = get_vector_store()
    assert isinstance(store, InMemoryVectorStore)


def test_chroma_failure_does_not_fallback_to_memory(monkeypatch):
    import services.rag.vector_store as vs

    monkeypatch.setattr(config, "KNOWLEDGE_VECTOR_STORE", "chroma")
    monkeypatch.setattr(vs, "_CHROMA_STORE", None)
    monkeypatch.setattr(
        vs, "ChromaVectorStore",
        lambda: (_ for _ in ()).throw(RuntimeError("chroma down")),
    )
    with pytest.raises(RuntimeError, match="持久化向量库不可用"):
        vs.get_vector_store()


def test_chroma_dimension_change_uses_shadow_collection(monkeypatch):
    import services.rag.vector_store as vs

    class FakeCollection:
        def __init__(self, name, metadata, embeddings=None):
            self.name = name
            self.metadata = metadata
            self.embeddings = list(embeddings or [])

        def count(self):
            return len(self.embeddings)

        def get(self, limit=None, include=None):
            rows = self.embeddings[:limit]
            return {"embeddings": rows, "ids": [str(i) for i in range(len(rows))]}

        def modify(self, metadata):
            self.metadata = dict(metadata)

    class FakeClient:
        def __init__(self):
            self.collections = {
                # metadata 故意谎报 7，真实向量维度是 5。
                "kb": FakeCollection("kb", {"dimension": 7}, [[0.0] * 5, [1.0] * 5])
            }

        def list_collections(self):
            return list(self.collections.values())

        def get_collection(self, name):
            if name not in self.collections:
                raise KeyError(name)
            return self.collections[name]

        def get_or_create_collection(self, name, metadata):
            return self.collections.setdefault(name, FakeCollection(name, metadata))

    client = FakeClient()
    monkeypatch.setitem(
        sys.modules,
        "chromadb",
        SimpleNamespace(PersistentClient=lambda path: client),
    )
    monkeypatch.setattr(vs, "EMBEDDING_DIM", 7)

    store = vs.ChromaVectorStore(persist_dir="unused", collection="kb")

    assert store.dimension_migration_required is True
    assert store.migration_required is True
    assert store.collection_name == vs._embedding_collection_name(
        "kb", store.embedding_fingerprint
    )
    assert client.collections["kb"].count() == 2
    assert client.collections[store.collection_name].metadata["dimension"] == 7
    with pytest.raises(RuntimeError, match="全量重索引"):
        store.query([0.0] * 7, 1)


def test_real_chroma_detects_lying_legacy_dimension_and_same_dim_model_change(
    tmp_path, monkeypatch
):
    chromadb = pytest.importorskip("chromadb")
    import services.rag.vector_store as vs

    persist_dir = str(tmp_path / "chroma-fingerprint")
    client = chromadb.PersistentClient(path=persist_dir)
    legacy = client.get_or_create_collection(
        "kb_test", metadata={"hnsw:space": "cosine", "dimension": 7}
    )
    legacy.add(
        ids=["legacy"], documents=["旧向量"], embeddings=[[0.0] * 5],
        metadatas=[{"status": "canonical"}],
    )
    monkeypatch.setattr(vs, "EMBEDDING_DIM", 7)
    monkeypatch.setattr(vs, "EMBEDDING_MODEL", "model-a")

    migrating = vs.ChromaVectorStore(persist_dir=persist_dir, collection="kb_test")
    assert migrating.dimension_migration_required is True
    assert migrating.migration_required is True

    old_dim_dir = str(tmp_path / "chroma-old-dim-name")
    old_dim_client = chromadb.PersistentClient(path=old_dim_dir)
    old_dim = old_dim_client.get_or_create_collection(
        "kb_test__dim_7", metadata={"hnsw:space": "cosine", "dimension": 7}
    )
    old_dim.add(
        ids=["old-dim"], documents=["旧版命名"], embeddings=[[0.0] * 7],
        metadatas=[{"status": "canonical"}],
    )
    old_dim_migration = vs.ChromaVectorStore(
        persist_dir=old_dim_dir, collection="kb_test"
    )
    assert old_dim_migration.migration_required is True

    # 新库上先激活 model-a，再换成同维 model-b；两者仍必须隔离。
    clean_dir = str(tmp_path / "chroma-same-dim")
    store_a = vs.ChromaVectorStore(persist_dir=clean_dir, collection="kb_test")
    store_a.add(
        ["a"], ["A"], [[1.0] * 7], [{"status": "canonical"}]
    )
    name_a = store_a.collection_name
    restarted_a = vs.ChromaVectorStore(persist_dir=clean_dir, collection="kb_test")
    assert restarted_a.migration_required is False
    assert restarted_a.collection_name == name_a
    monkeypatch.setattr(vs, "EMBEDDING_MODEL", "model-b")
    store_b = vs.ChromaVectorStore(persist_dir=clean_dir, collection="kb_test")
    assert store_b.collection_name != name_a
    assert store_b.migration_required is True
    assert store_b.count() == 0
    assert store_b._client.get_collection(name=name_a).count() == 1

    store_b.prepare_rebuild()
    store_b.add_rebuild(
        ["b"], ["B"], [[0.5] * 7], [{"status": "canonical"}]
    )
    store_b.activate_rebuild()
    assert store_b.migration_required is False
    assert store_b.query([0.5] * 7, 1)[0]["id"] == "b"
    restarted_b = vs.ChromaVectorStore(persist_dir=clean_dir, collection="kb_test")
    assert restarted_b.migration_required is False
    assert restarted_b.collection_name == store_b.collection_name


def test_embedding_migration_switches_sql_only_after_whole_shadow_succeeds(
    db, fake_embed
):
    from services.rag.knowledge_base import KnowledgeBaseService, KnowledgeIndexError

    old_store = InMemoryVectorStore()
    original = _kb(db, old_store, fake_embed)
    original.create_from_upload(
        title="文档 A", category="内部规范", filename="a.md",
        raw=METHODOLOGY_MD.encode("utf-8"),
    )
    original.create_from_upload(
        title="文档 B", category="外部指标", filename="b.md",
        raw=TREND_MD.encode("utf-8"),
    )
    old_ids = {row.vector_id for row in db.query(KnowledgeChunk).all()}

    class MigratingStore(InMemoryVectorStore):
        migration_required = True
        activated = False
        collection_name = "kb__emb_new"

        def prepare_rebuild(self):
            self.reset()

        def add_rebuild(self, ids, documents, embeddings, metadatas):
            super().add(ids, documents, embeddings, metadatas)

        def activate_rebuild(self):
            self.migration_required = False
            self.activated = True

    target = MigratingStore()
    calls = 0

    def fail_second_document(texts):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second document failed")
        return fake_embed(texts)

    svc = KnowledgeBaseService(db, store=target, embed_func=fail_second_document)
    with pytest.raises(KnowledgeIndexError, match="构建失败"):
        svc.reindex()
    assert {row.vector_id for row in db.query(KnowledgeChunk).all()} == old_ids
    assert target.count() == 0
    assert target.activated is False

    svc._embed = fake_embed
    assert svc.reindex() == 2
    new_ids = {row.vector_id for row in db.query(KnowledgeChunk).all()}
    assert new_ids and new_ids.isdisjoint(old_ids)
    assert target.activated is True
    assert target.count() == len(new_ids)


def test_chroma_failure_keeps_sql_knowledge_reads_available(db, monkeypatch):
    import services.rag.knowledge_base as kb
    from services.rag.knowledge_base import KnowledgeBaseService, KnowledgeIndexError

    monkeypatch.setattr(
        kb, "get_vector_store",
        lambda: (_ for _ in ()).throw(RuntimeError("chroma down")),
    )
    svc = KnowledgeBaseService(db)
    assert svc.list_items() == []
    with pytest.raises(KnowledgeIndexError, match="持久化向量库不可用"):
        svc.ensure_vector_available()


def test_embedding_migration_blocks_normal_vector_traffic_but_allows_recovery(db):
    from services.rag.knowledge_base import KnowledgeBaseService, KnowledgeIndexError

    store = InMemoryVectorStore()
    store.migration_required = True
    svc = KnowledgeBaseService(db, store=store)
    with pytest.raises(KnowledgeIndexError, match="全量重索引"):
        svc.ensure_vector_available()
    svc.ensure_vector_available(allow_migration=True)


@pytest.mark.parametrize("message", ["Embedding 配置已变更，必须全量重索引", "影子索引激活失败"])
def test_sync_reindex_reports_whole_operation_failure(app_client, monkeypatch, message):
    from services.rag.knowledge_base import KnowledgeBaseService, KnowledgeIndexError
    def fail(self, category=None):
        raise KnowledgeIndexError(message)
    monkeypatch.setattr(KnowledgeBaseService, "reindex", fail)
    for _ in range(2):  # 异常也必须释放操作锁，第二次不能变成429。
        response = app_client.post("/api/knowledge/reindex", json={})
        assert response.status_code == 503
        assert response.json()["detail"] == message


def test_retired_chroma_cleanup_and_document_delete_retry(db, tmp_path, monkeypatch):
    pytest.importorskip("chromadb")
    import services.rag.vector_store as vs
    import services.rag.knowledge_base as kb
    from models import KnowledgeDocument
    monkeypatch.setattr(kb, "KNOWLEDGE_DATA_DIR", str(tmp_path / "sources"))
    monkeypatch.setattr(vs, "EMBEDDING_DIM", 2)
    monkeypatch.setattr(vs, "EMBEDDING_MODEL", "model-a")
    directory = str(tmp_path / "chroma")
    a = vs.ChromaVectorStore(persist_dir=directory, collection="cleanup_test")
    embed = lambda texts: [[1.0, 0.0] for _ in texts]
    svc = kb.KnowledgeBaseService(db, store=a, embed_func=embed)
    doc = svc.create_from_upload(title="迁移删除", category="内部规范", filename="a.txt", raw="模拟原文".encode())
    item_id = db.query(KnowledgeItem).filter_by(document_id=doc.id).one().id
    # 相似前缀的其他库及明确属于其他库的集合不可被删除。
    unrelated = a._client.get_or_create_collection("cleanup_test__other")
    other_owned = a._client.get_or_create_collection(
        "cleanup_test__dim_99", metadata={"base_collection": "another_kb"})
    monkeypatch.setattr(vs, "EMBEDDING_MODEL", "model-b")
    b = vs.ChromaVectorStore(persist_dir=directory, collection="cleanup_test")
    svc._store = b
    delete_collection = b._client.delete_collection
    def fail_retirement(name):
        if name == a.collection_name:
            raise RuntimeError("模拟旧集合占用")
        return delete_collection(name=name)
    monkeypatch.setattr(b._client, "delete_collection", fail_retirement)
    with pytest.raises(kb.KnowledgeIndexError, match="历史索引清理失败"):
        svc.reindex()
    assert not b.migration_required and b.retired_cleanup_pending
    assert not svc.inspect_vector_health()["consistent"]
    assert a.count() == 1 and b.count() == 1

    # 清理失败时删除仍须覆盖旧版本；任一旧集合删除失败必须保留tombstone。
    related = list(b._related_collections())
    old = next(c for c in related if c.name == a.collection_name)
    original_delete = old.delete
    monkeypatch.setattr(old, "delete", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("模拟删除失败")))
    monkeypatch.setattr(b, "_related_collections", lambda: iter(related))
    with pytest.raises(kb.KnowledgeIndexError, match="知识已下线"):
        svc.delete_item(item_id)
    assert db.get(KnowledgeDocument, doc.id).index_status == "deleting"
    monkeypatch.setattr(old, "delete", original_delete)
    assert svc.retry_pending_deletes()["pending_delete_retried"] == 1
    assert db.get(KnowledgeDocument, doc.id) is None
    assert a.count() == b.count() == 0
    monkeypatch.setattr(b._client, "delete_collection", delete_collection)
    assert kb.run_vector_maintenance_once(db, b) is True
    assert not b.retired_cleanup_pending and svc.inspect_vector_health()["consistent"]
    names = {c.name for c in b._client.list_collections()}
    assert a.collection_name not in names
    assert {b.collection_name, unrelated.name, other_owned.name} <= names


def test_readiness_returns_503_when_chroma_is_unavailable(monkeypatch):
    import main
    import services.rag.vector_store as vs

    monkeypatch.setattr(
        vs, "get_vector_store",
        lambda: (_ for _ in ()).throw(RuntimeError("chroma down")),
    )
    response = main.readiness()
    assert response.status_code == 503
    assert b'"ready":false' in response.body


def test_readiness_rejects_sql_vector_split_brain(
    db, memory_store, fake_embed, monkeypatch
):
    import json
    import main
    import services.rag.vector_store as vs
    from services.rag.knowledge_base import KnowledgeBaseService

    svc = KnowledgeBaseService(db, store=memory_store, embed_func=fake_embed)
    doc = svc.create_from_upload(
        title="持久化缺失探针", category="内部规范", filename="probe.md",
        raw=METHODOLOGY_MD.encode("utf-8"),
    )
    vector_ids = list(memory_store.list_ids())
    memory_store.delete(vector_ids)
    before = svc.inspect_vector_integrity()
    db.refresh(doc)

    monkeypatch.setattr(vs, "get_vector_store", lambda: memory_store)
    monkeypatch.setattr(
        main, "SessionLocal",
        sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False),
    )
    response = main.readiness()
    body = json.loads(response.body)

    assert response.status_code == 503
    assert body["integrity"]["missing_vectors"] == len(vector_ids)
    assert body["integrity"]["affected_documents"] == [doc.id]
    # readiness 必须只读；状态校正由启动对账或显式 reconcile 负责。
    assert set(before["missing_ids"]) == set(vector_ids)
    assert doc.index_status == "indexed"


def test_readiness_rejects_pending_metadata_outbox(db, memory_store, monkeypatch):
    import json
    import main
    import services.rag.vector_store as vs

    db.add(KnowledgeVectorMetadataOutbox(vector_id="pending", payload={"status": "canonical"}))
    db.commit()
    monkeypatch.setattr(vs, "get_vector_store", lambda: memory_store)
    monkeypatch.setattr(
        main, "SessionLocal",
        sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False),
    )

    response = main.readiness()
    body = json.loads(response.body)

    assert response.status_code == 503
    assert body["integrity"]["metadata_pending"] == 1


def test_readiness_reuses_background_integrity_snapshot(
    db, memory_store, fake_embed, monkeypatch
):
    import main
    import services.rag.vector_store as vs
    from services.rag.knowledge_base import KnowledgeBaseService

    svc = _kb(db, memory_store, fake_embed)
    svc.create_from_upload(
        title="轻量探针", category="内部规范", filename="probe.md",
        raw=METHODOLOGY_MD.encode("utf-8"),
    )
    svc.inspect_vector_integrity()
    calls = 0
    original_list_ids = memory_store.list_ids

    def counted_list_ids():
        nonlocal calls
        calls += 1
        return original_list_ids()

    monkeypatch.setattr(memory_store, "list_ids", counted_list_ids)
    monkeypatch.setattr(vs, "get_vector_store", lambda: memory_store)
    monkeypatch.setattr(
        main, "SessionLocal",
        sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False),
    )
    assert main.readiness()["ready"] is True
    assert main.readiness()["ready"] is True
    assert calls == 0


# ══════════════════════════ 重排 ════════════════════════════════════════════


def test_metadata_reranker_boosts_internal_norm():
    reranker = MetadataReranker()
    candidates = [
        RerankCandidate(id="ext", content="行业趋势", metadata={"category": "外部指标", "title": "t"}, base_score=0.9),
        RerankCandidate(id="norm", content="客户分级标准", metadata={"category": "内部规范", "title": "t"}, base_score=0.9),
    ]
    ranked = reranker.rerank("客户分级标准", candidates)
    assert ranked[0].id == "norm"  # 内部规范权重 1.3 > 外部 1.0


def test_metadata_reranker_industry_boost():
    reranker = MetadataReranker()
    candidates = [
        RerankCandidate(id="other", content="x", metadata={"category": "内部案例", "industry": "金融", "title": "t"}, base_score=0.8),
        RerankCandidate(id="same", content="x", metadata={"category": "内部案例", "industry": "制造", "title": "t"}, base_score=0.8),
    ]
    ranked = reranker.rerank("查询", candidates, boost_industry="制造")
    assert ranked[0].id == "same"


# ══════════════════════════ 检索 ════════════════════════════════════════════


def _seed_store(store):
    docs = [
        ("内部规范", "canonical", "回款逾期应催收并评估风险"),
        ("外部指标", "canonical", "行业增速放缓需关注"),
        ("内部规范", "proposed", "未审核的草稿不应被检索命中"),
    ]
    ids, texts, vecs, metas = [], [], [], []
    for i, (cat, status, text) in enumerate(docs):
        ids.append(f"d{i}:0")
        texts.append(text)
        vecs.append(_bag_of_chars_embed([text])[0])
        metas.append({"document_id": i, "chunk_index": 0, "category": cat, "title": f"doc{i}", "status": status, "item_id": i, "source_type": "文档"})
    store.add(ids, texts, vecs, metas)
    return store


def test_retrieve_filters_canonical_and_topk(memory_store):
    _seed_store(memory_store)
    results = retrieve_knowledge("回款风险", embed_func=_bag_of_chars_embed, store=memory_store, top_k=2)
    assert len(results) == 2
    assert all(r.metadata["status"] == "canonical" for r in results)
    assert isinstance(results[0], RetrievedChunk)
    # 回款相关应排前
    assert "回款" in results[0].content


def test_retrieve_status_all_includes_proposed(memory_store):
    _seed_store(memory_store)
    results = retrieve_knowledge("草稿", embed_func=_bag_of_chars_embed, store=memory_store, status=None)
    assert any("未审核" in r.content for r in results)


def test_retrieve_embedding_unavailable_degrades(memory_store, monkeypatch):
    def _boom(texts):
        raise EmbeddingUnavailableError("no key")

    results = retrieve_knowledge("查询", embed_func=_boom, store=memory_store)
    assert results == []  # 静默降级


# ══════════════════════════ 知识库服务 ══════════════════════════════════════


def _kb(db, memory_store, fake_embed):
    from services.rag.knowledge_base import KnowledgeBaseService

    return KnowledgeBaseService(db, store=memory_store, embed_func=fake_embed)


def test_upload_index_and_search(db, memory_store, fake_embed):
    svc = _kb(db, memory_store, fake_embed)
    doc = svc.create_from_upload(
        title="评估方法论", category="内部规范", filename="m.md", raw=METHODOLOGY_MD.encode("utf-8")
    )
    assert doc.index_status == "indexed"
    assert doc.chunk_count > 0
    assert memory_store.count() == doc.chunk_count

    item = db.query(KnowledgeItem).filter(KnowledgeItem.document_id == doc.id).first()
    svc.approve_item(item.id)  # 审核后才能被默认检索命中
    results = svc.search("回款逾期如何处置")
    assert results
    assert any("回款" in r.content for r in results)


def test_update_metadata_syncs_vector(memory_store, db, fake_embed):
    svc = _kb(db, memory_store, fake_embed)
    doc = svc.create_from_upload(
        title="方法论", category="内部规范", filename="m.md", raw=METHODOLOGY_MD.encode("utf-8")
    )
    item = db.query(KnowledgeItem).filter(KnowledgeItem.document_id == doc.id).first()
    svc.update_item_metadata(item.id, category="外部指标")
    db.refresh(item)
    assert item.category == "外部指标"
    # 向量 metadata 已同步
    after = memory_store.query(_bag_of_chars_embed(["评估"])[0], top_k=5, where={"category": "外部指标"})
    assert after  # 现在能按新分类过滤到


def test_delete_cleans_vectors(memory_store, db, fake_embed):
    svc = _kb(db, memory_store, fake_embed)
    doc = svc.create_from_upload(
        title="方法论", category="内部规范", filename="m.md", raw=METHODOLOGY_MD.encode("utf-8")
    )
    item = db.query(KnowledgeItem).filter(KnowledgeItem.document_id == doc.id).first()
    before = memory_store.count()
    assert before > 0
    assert svc.delete_item(item.id) is True
    assert memory_store.count() == 0


def test_delete_vector_failure_keeps_durable_tombstone(memory_store, db, fake_embed, monkeypatch):
    from models import KnowledgeDocument
    from services.rag.knowledge_base import KnowledgeIndexError

    svc = _kb(db, memory_store, fake_embed)
    doc = svc.create_from_upload(
        title="敏感资料", category="内部规范", filename="m.md", raw=METHODOLOGY_MD.encode("utf-8")
    )
    item = db.query(KnowledgeItem).filter(KnowledgeItem.document_id == doc.id).one()
    svc.approve_item(item.id)
    original_delete = memory_store.delete
    monkeypatch.setattr(memory_store, "delete", lambda ids: (_ for _ in ()).throw(RuntimeError("down")))

    with pytest.raises(KnowledgeIndexError, match="知识已下线"):
        svc.delete_item(item.id)
    db.refresh(item)
    db.refresh(doc)
    assert item.status == "proposed"
    assert doc.status == "proposed"
    assert doc.index_status == "deleting"
    assert svc.search("回款逾期") == []

    monkeypatch.setattr(memory_store, "delete", original_delete)
    result = svc.reconcile_vectors(cleanup_orphans=True)
    assert result["pending_delete_retried"] == 1
    assert result["pending_delete_failures"] == []
    assert db.get(KnowledgeDocument, doc.id) is None
    assert memory_store.count() == 0


def test_reindex_never_resurrects_failed_delete_tombstone(
    memory_store, db, fake_embed, monkeypatch
):
    from models import KnowledgeDocument
    from services.rag.knowledge_base import KnowledgeIndexError

    svc = _kb(db, memory_store, fake_embed)
    doc = svc.create_from_upload(
        title="待删除资料", category="内部规范", filename="m.md",
        raw=METHODOLOGY_MD.encode("utf-8"),
    )
    item = db.query(KnowledgeItem).filter(KnowledgeItem.document_id == doc.id).one()
    monkeypatch.setattr(
        memory_store, "delete",
        lambda ids: (_ for _ in ()).throw(RuntimeError("down")),
    )
    with pytest.raises(KnowledgeIndexError):
        svc.delete_item(item.id)

    assert svc.reindex() == 0
    db.expire_all()
    tombstone = db.get(KnowledgeDocument, doc.id)
    assert tombstone is not None
    assert tombstone.index_status == "deleting"


def test_reconcile_preserves_delete_intent_when_vectors_are_missing(
    memory_store, db, fake_embed, monkeypatch
):
    from pathlib import Path
    from models import KnowledgeDocument
    from services.rag.knowledge_base import KnowledgeIndexError

    svc = _kb(db, memory_store, fake_embed)
    doc = svc.create_from_upload(
        title="待清理文档", category="内部规范", filename="delete.md",
        raw=METHODOLOGY_MD.encode("utf-8"),
    )
    doc_id, file_path = doc.id, Path(doc.file_path)
    item = db.query(KnowledgeItem).filter(KnowledgeItem.document_id == doc_id).one()
    memory_store.reset()  # 删除之前索引已损坏；不能因此丢失后续删除意图。
    with monkeypatch.context() as failure:
        failure.setattr(
            memory_store, "delete",
            lambda ids: (_ for _ in ()).throw(RuntimeError("delete unavailable")),
        )
        with pytest.raises(KnowledgeIndexError):
            svc.delete_item(item.id)
        result = svc.reconcile_vectors(cleanup_orphans=True)
        assert result["pending_delete_failures"]
        db.expire_all()
        assert db.get(KnowledgeDocument, doc_id).index_status == "deleting"
        assert svc.reindex() == 0

    recovered = svc.reconcile_vectors(cleanup_orphans=True)
    assert recovered["pending_delete_retried"] == 1
    assert db.get(KnowledgeDocument, doc_id) is None
    assert not file_path.exists()


def test_reconcile_waits_for_shadow_index_commit(memory_store, db, fake_embed, monkeypatch):
    """对账清理必须与影子索引共享锁，不能把 SQL 切换前的 staged ID 当孤儿删掉。"""
    svc = _kb(db, memory_store, fake_embed)
    doc = svc.create_from_upload(
        title="并发重索引", category="内部规范", filename="m.md",
        raw=METHODOLOGY_MD.encode("utf-8"),
    )
    original_add = memory_store.add
    staged = threading.Event()
    release = threading.Event()

    def blocking_add(ids, documents, embeddings, metadatas):
        original_add(ids, documents, embeddings, metadatas)
        if any(":v" in vector_id for vector_id in ids):
            staged.set()
            assert release.wait(2)

    monkeypatch.setattr(memory_store, "add", blocking_add)
    errors: list[BaseException] = []

    def reindex_worker():
        try:
            svc.reindex()
        except BaseException as exc:  # pragma: no cover - 失败由主线程断言
            errors.append(exc)

    reconcile_result: dict = {}

    def reconcile_worker():
        try:
            reconcile_result.update(svc.reconcile_vectors(cleanup_orphans=True))
        except BaseException as exc:  # pragma: no cover - 失败由主线程断言
            errors.append(exc)

    reindex_thread = threading.Thread(target=reindex_worker)
    reconcile_thread = threading.Thread(target=reconcile_worker)
    reindex_thread.start()
    assert staged.wait(2)
    reconcile_thread.start()
    time.sleep(0.05)
    assert reconcile_thread.is_alive()
    release.set()
    reindex_thread.join(2)
    reconcile_thread.join(2)
    assert not errors
    assert reconcile_result["missing_ids"] == []
    db.refresh(doc)
    assert doc.index_status == "indexed"


def test_revoke_uses_sql_status_when_vector_metadata_update_fails(
    memory_store, db, fake_embed, monkeypatch
):
    svc = _kb(db, memory_store, fake_embed)
    doc = svc.create_from_upload(
        title="待撤销资料", category="内部规范", filename="m.md", raw=METHODOLOGY_MD.encode("utf-8")
    )
    item = db.query(KnowledgeItem).filter(KnowledgeItem.document_id == doc.id).one()
    svc.approve_item(item.id)
    original_update = memory_store.update_metadatas
    monkeypatch.setattr(
        memory_store, "update_metadatas",
        lambda ids, metas: (_ for _ in ()).throw(RuntimeError("down")),
    )
    revoked = svc.revoke_item(item.id)
    assert revoked is not None and revoked.sync_pending is True
    assert revoked.sync_warnings
    assert db.query(KnowledgeVectorMetadataOutbox).count() == doc.chunk_count
    assert svc.search("回款逾期") == []

    monkeypatch.setattr(memory_store, "update_metadatas", original_update)
    from services.rag.knowledge_base import run_vector_maintenance_once

    assert run_vector_maintenance_once(db, memory_store) is True
    result = svc.inspect_vector_health()
    assert result["metadata_pending"] == 0
    assert not memory_store.query(
        _bag_of_chars_embed(["回款逾期"])[0],
        top_k=10,
        where={"status": "canonical"},
    )


def test_vector_reconcile_cleans_orphans(memory_store, db, fake_embed):
    svc = _kb(db, memory_store, fake_embed)
    memory_store.add(["orphan"], ["孤儿"], [[1.0] * 64], [{"status": "canonical"}])
    result = svc.reconcile_vectors(cleanup_orphans=True)
    assert result["orphan_ids"] == []
    assert result["orphans_cleaned"] == 1
    assert "orphan" not in memory_store.list_ids()


def test_reindex(db, memory_store, fake_embed):
    svc = _kb(db, memory_store, fake_embed)
    doc = svc.create_from_upload(
        title="方法论", category="内部规范", filename="m.md", raw=METHODOLOGY_MD.encode("utf-8")
    )
    # 破坏向量后重索引应恢复
    memory_store.reset()
    assert memory_store.count() == 0
    n = svc.reindex()
    assert n == 1
    assert memory_store.count() == doc.chunk_count


def test_adopt_strategy_is_canonical(db, memory_store, fake_embed):
    svc = _kb(db, memory_store, fake_embed)
    doc = svc.create_from_strategy(
        title="采纳的策略", strategy_text="建议安排高层拜访以应对竞品。", adopted_by="alice", customer_name="示例汽车集团"
    )
    assert doc.category == "对话沉淀"
    assert doc.status == "canonical"
    assert doc.index_status == "indexed"


# ══════════════════════════ 知识上下文注入对话 ══════════════════════════════


def test_build_knowledge_context_injects_refs(db, memory_store, fake_embed):
    from services.ai import context_builder

    svc = _kb(db, memory_store, fake_embed)
    doc = svc.create_from_upload(
        title="方法论", category="内部规范", filename="m.md", raw=METHODOLOGY_MD.encode("utf-8")
    )
    item = db.query(KnowledgeItem).filter(KnowledgeItem.document_id == doc.id).first()
    svc.approve_item(item.id)

    text, refs = context_builder.build_knowledge_context(
        "回款风险", db=db, embed_func=fake_embed, store=memory_store
    )
    assert "知识库参考资料" in text
    assert refs
    assert refs[0]["category"] == "内部规范"
    assert "document_id" in refs[0]


def test_build_knowledge_context_degrades_without_query(db, memory_store, fake_embed):
    from services.ai import context_builder

    text, refs = context_builder.build_knowledge_context("", db=db)
    assert text == context_builder.NO_KNOWLEDGE_HINT
    assert refs == []


# ══════════════════════════ 接口层═══════════════════════════════


@pytest.fixture()
def app_client(session_factory, monkeypatch):
    from routers import customers as customers_router
    from routers import knowledge as knowledge_router

    # 用假 embedding 替换三处引用，避免依赖真实 API
    def _fake_factory():
        return _bag_of_chars_embed

    monkeypatch.setattr("services.rag.embeddings.make_embedding_func", _fake_factory)
    monkeypatch.setattr("services.rag.knowledge_base.make_embedding_func", _fake_factory)
    monkeypatch.setattr("services.rag.retriever.make_embedding_func", _fake_factory)
    monkeypatch.setattr(config, "KNOWLEDGE_VECTOR_STORE", "memory")
    import services.rag.vector_store as vs

    vs._MEMORY_STORE = None

    app = FastAPI()
    app.include_router(knowledge_router.router, prefix="/api")
    app.include_router(customers_router.router, prefix="/api")

    def _override_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as c:
        yield c


def test_upload_and_list_and_search(app_client):
    files = {"file": ("methodology.md", io.BytesIO(METHODOLOGY_MD.encode("utf-8")), "text/markdown")}
    resp = app_client.post(
        "/api/knowledge/upload", files=files, data={"title": "方法论", "category": "内部规范"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["index_status"] == "indexed"
    item_id = body["item_id"]

    # 列表
    lst = app_client.get("/api/knowledge/items")
    assert lst.status_code == 200
    assert lst.json()["total"] >= 1

    # 审核后检索命中
    app_client.post(f"/api/knowledge/items/{item_id}/approve")
    search = app_client.post("/api/knowledge/search", json={"query": "回款风险", "top_k": 3})
    assert search.status_code == 200
    results = search.json()["results"]
    assert results
    assert any("回款" in r["content"] for r in results)


def test_search_status_all_includes_proposed(app_client):
    files = {"file": ("trend.md", io.BytesIO(TREND_MD.encode("utf-8")), "text/markdown")}
    app_client.post("/api/knowledge/upload", files=files, data={"category": "外部指标"})
    # 未审核（proposed）默认检索不到，status=all 能检索到
    default = app_client.post("/api/knowledge/search", json={"query": "行业趋势", "top_k": 3})
    assert default.json()["results"] == []
    alls = app_client.post("/api/knowledge/search", json={"query": "行业趋势", "top_k": 3, "status": "all"})
    assert alls.json()["results"]


def test_update_approve_delete_reindex_status(app_client):
    files = {"file": ("m.md", io.BytesIO(METHODOLOGY_MD.encode("utf-8")), "text/markdown")}
    up = app_client.post("/api/knowledge/upload", files=files, data={"category": "内部规范"})
    item_id = up.json()["item_id"]

    upd = app_client.put(f"/api/knowledge/items/{item_id}", json={"category": "外部指标", "tags": ["a", "b"]})
    assert upd.status_code == 200
    assert upd.json()["category"] == "外部指标"

    app_client.post(f"/api/knowledge/items/{item_id}/approve")
    reidx = app_client.post("/api/knowledge/reindex", json={})
    assert reidx.status_code == 200
    assert reidx.json()["reindexed"] >= 1

    status = app_client.get("/api/knowledge/status")
    assert status.status_code == 200
    assert status.json()["vector_store"] == "InMemoryVectorStore"

    dele = app_client.delete(f"/api/knowledge/items/{item_id}")
    assert dele.status_code == 200 and dele.json()["deleted"] is True
    assert app_client.get("/api/knowledge/items").json()["total"] == 0


def test_async_reindex_job_reports_completion(app_client):
    files = {"file": ("async.md", io.BytesIO(METHODOLOGY_MD.encode("utf-8")), "text/markdown")}
    app_client.post("/api/knowledge/upload", files=files, data={"category": "内部规范"})

    created = app_client.post("/api/knowledge/reindex/jobs", json={})
    assert created.status_code == 200
    assert created.json()["status"] == "running"

    status = app_client.get(f"/api/knowledge/reindex/jobs/{created.json()['job_id']}")
    assert status.status_code == 200
    assert status.json()["status"] == "ready"
    assert status.json()["reindexed"] >= 1


def _wait_reindex_job(app_client, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = app_client.get(f"/api/knowledge/reindex/jobs/{job_id}")
        assert response.status_code == 200
        result = response.json()
        if result["status"] != "running":
            return result
        time.sleep(0.01)
    pytest.fail("重索引任务未能结束")


def test_async_reindex_recovers_embedding_migration(app_client, monkeypatch):
    uploaded = app_client.post(
        "/api/knowledge/upload",
        files={"file": ("migration.md", METHODOLOGY_MD.encode("utf-8"), "text/markdown")},
        data={"category": "内部规范"},
    )
    assert uploaded.status_code == 200

    class MigratingStore(InMemoryVectorStore):
        migration_required = True

        def prepare_rebuild(self):
            self.reset()

        def add_rebuild(self, ids, documents, embeddings, metadatas):
            super().add(ids, documents, embeddings, metadatas)

        def activate_rebuild(self):
            self.migration_required = False

    target = MigratingStore()
    monkeypatch.setattr("services.rag.knowledge_base.get_vector_store", lambda: target)
    created = app_client.post("/api/knowledge/reindex/jobs", json={})
    assert created.status_code == 200
    result = _wait_reindex_job(app_client, created.json()["job_id"])
    assert result["status"] == "ready", result
    assert result["reindexed"] == 1
    assert target.migration_required is False
    assert target.count() == uploaded.json()["chunk_count"]


def test_async_reindex_reports_partial_document_failures(app_client, db):
    from pathlib import Path
    from models import KnowledgeDocument

    for title in ("可重建文档", "原文缺失文档"):
        response = app_client.post(
            "/api/knowledge/upload",
            files={"file": ("partial.md", METHODOLOGY_MD.encode("utf-8"), "text/markdown")},
            data={"title": title, "category": "内部规范"},
        )
        assert response.status_code == 200
    missing = db.query(KnowledgeDocument).filter(KnowledgeDocument.title == "原文缺失文档").one()
    missing_id = missing.id
    Path(missing.file_path).unlink()
    created = app_client.post("/api/knowledge/reindex/jobs", json={})
    assert created.status_code == 200
    result = _wait_reindex_job(app_client, created.json()["job_id"])
    assert result["status"] == "partial", result
    assert result["reindexed"] == 1
    assert result["failed_documents"] == [{
        "document_id": missing_id, "title": "原文缺失文档", "error": "文档无可用文本",
    }]


def test_sync_reindex_rejects_when_another_reindex_is_running(app_client):
    from routers import knowledge as knowledge_router

    assert knowledge_router._reindex_operation_lock.acquire(blocking=False)
    try:
        response = app_client.post("/api/knowledge/reindex", json={})
    finally:
        knowledge_router._reindex_operation_lock.release()

    assert response.status_code == 429
    assert response.json()["detail"] == "索引正在重建，请等待当前任务完成"


def test_reindex_job_sweep_marks_stale_running_job_as_error(monkeypatch):
    from routers import knowledge as knowledge_router

    knowledge_router._reindex_jobs.clear()
    knowledge_router._reindex_jobs["stale"] = {
        "status": "running",
        "created": 100.0,
        "reindexed": 0,
        "error": None,
    }
    monkeypatch.setattr(knowledge_router, "_REINDEX_JOB_TTL", 60)

    knowledge_router._sweep_reindex_jobs(now=161.0)

    assert knowledge_router._reindex_jobs["stale"]["status"] == "error"
    assert knowledge_router._reindex_jobs["stale"]["error"] == "重建索引超时"
    knowledge_router._reindex_jobs.clear()


# ══════════════════ 分类规范化与历史数据迁移（2026-08 第二轮修复）═══════════════════


def test_normalize_category_alias_and_invalid():
    from services.rag.knowledge_base import normalize_category

    # 旧版前端分类名自动校正为规范名
    assert normalize_category("公司内部规范") == "内部规范"
    assert normalize_category("内部数据指标") == "内部指标"
    assert normalize_category("外部数据指标") == "外部指标"
    assert normalize_category("对话沉淀") == "对话沉淀"
    # 规范名原样通过
    assert normalize_category("内部规范") == "内部规范"
    # 非法分类抛 ValueError
    with pytest.raises(ValueError):
        normalize_category("不存在的分类")


def test_upload_invalid_category_400(app_client):
    files = {"file": ("m.md", io.BytesIO(METHODOLOGY_MD.encode("utf-8")), "text/markdown")}
    resp = app_client.post("/api/knowledge/upload", files=files, data={"category": "乱写的分类"})
    assert resp.status_code == 400


def test_upload_legacy_category_normalized(app_client):
    """旧分类名上传时自动校正为规范名（保证权重表命中）。"""
    files = {"file": ("m.md", io.BytesIO(METHODOLOGY_MD.encode("utf-8")), "text/markdown")}
    resp = app_client.post("/api/knowledge/upload", files=files, data={"category": "公司内部规范"})
    assert resp.status_code == 200, resp.text
    item = app_client.get("/api/knowledge/items").json()["items"][0]
    assert item["category"] == "内部规范"


def test_upload_unsupported_extension_400(app_client):
    files = {"file": ("evil.exe", io.BytesIO(b"MZ\x90\x00"), "application/octet-stream")}
    resp = app_client.post("/api/knowledge/upload", files=files, data={"category": "内部规范"})
    assert resp.status_code == 400


def test_upload_requires_extension(app_client):
    files = {"file": ("README", io.BytesIO(b"plain text"), "text/plain")}
    resp = app_client.post("/api/knowledge/upload", files=files, data={"category": "内部规范"})
    assert resp.status_code == 400
    assert "扩展名" in resp.json()["detail"]


def test_upload_rejects_mime_and_magic_mismatch(app_client):
    wrong_mime = {
        "file": ("note.txt", io.BytesIO(b"plain text"), "application/pdf")
    }
    resp = app_client.post(
        "/api/knowledge/upload", files=wrong_mime, data={"category": "内部规范"}
    )
    assert resp.status_code == 400
    assert "MIME" in resp.json()["detail"]

    wrong_magic = {"file": ("fake.pdf", io.BytesIO(b"not a pdf"), "application/pdf")}
    resp = app_client.post(
        "/api/knowledge/upload", files=wrong_magic, data={"category": "内部规范"}
    )
    assert resp.status_code == 400
    assert "有效的 PDF" in resp.json()["detail"]


def test_upload_stops_after_byte_limit(app_client, monkeypatch):
    monkeypatch.setattr(config, "UPLOAD_MAX_BYTES", 8)
    files = {"file": ("large.txt", io.BytesIO(b"123456789"), "text/plain")}
    resp = app_client.post("/api/knowledge/upload", files=files, data={"category": "内部规范"})
    assert resp.status_code == 413


def test_upload_pipeline_concurrency_limit_returns_429(app_client, monkeypatch):
    import threading
    from routers import knowledge as knowledge_router

    semaphore = threading.BoundedSemaphore(1)
    monkeypatch.setattr(knowledge_router, "_UPLOAD_PIPELINE_SEM", semaphore)
    assert semaphore.acquire(blocking=False)
    try:
        files = {"file": ("queued.txt", io.BytesIO(b"text"), "text/plain")}
        response = app_client.post(
            "/api/knowledge/upload", files=files, data={"category": "内部规范"}
        )
    finally:
        semaphore.release()
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "5"


def test_index_limits_chunks_before_embedding(db, memory_store, monkeypatch):
    import services.rag.knowledge_base as kb

    called = False

    def tracking_embed(texts):
        nonlocal called
        called = True
        return _bag_of_chars_embed(texts)

    monkeypatch.setattr(kb, "UPLOAD_MAX_CHUNKS", 1)
    svc = kb.KnowledgeBaseService(db, store=memory_store, embed_func=tracking_embed)
    doc = svc.create_from_upload(
        title="超切片文档",
        category="内部规范",
        filename="large.md",
        raw=(("甲" * 600) + "。" + ("乙" * 600)).encode("utf-8"),
    )
    assert doc.index_status == "failed"
    assert "切片超过" in doc.index_error
    assert doc.chunk_count == 0
    assert called is False


def test_index_limits_extracted_chars_before_embedding(db, memory_store, monkeypatch):
    from services.rag.knowledge_base import KnowledgeBaseService

    called = False

    def tracking_embed(texts):
        nonlocal called
        called = True
        return _bag_of_chars_embed(texts)

    monkeypatch.setattr(config, "UPLOAD_MAX_EXTRACTED_CHARS", 5)
    svc = KnowledgeBaseService(db, store=memory_store, embed_func=tracking_embed)
    doc = svc.create_from_upload(
        title="超文本上限",
        category="内部规范",
        filename="large.txt",
        raw="超过五个字符的文本".encode("utf-8"),
    )
    assert doc.index_status == "failed"
    assert "抽取文本超过" in doc.index_error
    assert doc.chunk_count == 0
    assert called is False


def test_index_limits_estimated_tokens_before_embedding(db, memory_store, monkeypatch):
    import services.rag.knowledge_base as kb

    called = False

    def tracking_embed(texts):
        nonlocal called
        called = True
        return _bag_of_chars_embed(texts)

    monkeypatch.setattr(kb, "UPLOAD_MAX_EMBEDDING_TOKENS", 2)
    svc = kb.KnowledgeBaseService(db, store=memory_store, embed_func=tracking_embed)
    doc = svc.create_from_upload(
        title="超 Token 上限",
        category="内部规范",
        filename="tokens.txt",
        raw="多个中文字符".encode("utf-8"),
    )
    assert doc.index_status == "failed"
    assert "Embedding token" in doc.index_error
    assert called is False


def test_estimated_tokens_conservatively_counts_non_ascii():
    from services.rag.knowledge_base import _estimate_tokens

    # 旧算法把所有非中文字符都按英文的 4 字符/token 计算，emoji 可绕过上限。
    assert _estimate_tokens("abcd") == 1
    assert _estimate_tokens("中文") == 2
    assert _estimate_tokens("😀😀") == 8


def test_reindex_failure_preserves_last_good_index(
    db, memory_store, fake_embed, monkeypatch
):
    from models import KnowledgeChunk
    from services.rag.knowledge_base import KnowledgeBaseService, KnowledgeIndexError

    svc = KnowledgeBaseService(db, store=memory_store, embed_func=fake_embed)
    doc = svc.create_from_upload(
        title="重索引超限文档",
        category="内部规范",
        filename="reindex-limit.txt",
        raw=("测试文本。" * 100).encode("utf-8"),
    )
    assert doc.index_status == "indexed"
    assert doc.chunk_count > 0

    monkeypatch.setattr(config, "UPLOAD_MAX_EXTRACTED_CHARS", 5)
    old_count = doc.chunk_count
    old_vector_ids = {
        row.vector_id for row in db.query(KnowledgeChunk)
        .filter(KnowledgeChunk.document_id == doc.id).all()
    }
    with pytest.raises(KnowledgeIndexError) as raised:
        svc.reindex()
    db.refresh(doc)

    assert raised.value.succeeded == 0
    assert raised.value.failed[0]["document_id"] == doc.id
    assert doc.index_status == "indexed"
    assert doc.chunk_count == old_count
    assert {
        row.vector_id for row in db.query(KnowledgeChunk)
        .filter(KnowledgeChunk.document_id == doc.id).all()
    } == old_vector_ids


def test_office_zip_entry_limit(monkeypatch):
    import zipfile

    from services.rag.parser import validate_upload

    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr("[Content_Types].xml", "types")
        archive.writestr("word/document.xml", "document")
    monkeypatch.setattr(config, "UPLOAD_MAX_ZIP_ENTRIES", 1)
    with pytest.raises(ParseError, match="ZIP 条目"):
        validate_upload(
            "sample.docx",
            raw.getvalue(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )


def test_office_zip_compression_ratio_limit(monkeypatch):
    import zipfile

    from services.rag.parser import validate_upload

    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "x" * 20_000)
        archive.writestr("word/document.xml", "x" * 20_000)
    monkeypatch.setattr(config, "UPLOAD_MAX_COMPRESSION_RATIO", 2.0)
    with pytest.raises(ParseError, match="压缩比异常"):
        validate_upload(
            "sample.docx",
            raw.getvalue(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )


def test_update_metadata_invalid_category_400(app_client):
    files = {"file": ("m.md", io.BytesIO(METHODOLOGY_MD.encode("utf-8")), "text/markdown")}
    up = app_client.post("/api/knowledge/upload", files=files, data={"category": "内部规范"})
    item_id = up.json()["item_id"]
    resp = app_client.put(f"/api/knowledge/items/{item_id}", json={"category": "不合法"})
    assert resp.status_code == 400


def test_migrate_legacy_categories(db, memory_store, fake_embed):
    """历史数据中的旧分类名被一次性校正，且切片 metadata 副本同步更新。"""
    from models import KnowledgeDocument, KnowledgeItem
    from services.rag.knowledge_base import migrate_legacy_categories

    svc = _kb(db, memory_store, fake_embed)
    doc = svc.create_from_upload(
        title="旧文档", category="内部规范", filename="m.md", raw=METHODOLOGY_MD.encode("utf-8")
    )
    # 手工把分类改回旧名，模拟历史遗留数据
    db.query(KnowledgeDocument).filter(KnowledgeDocument.id == doc.id).update(
        {"category": "公司内部规范"}, synchronize_session=False
    )
    db.query(KnowledgeItem).filter(KnowledgeItem.document_id == doc.id).update(
        {"category": "公司内部规范"}, synchronize_session=False
    )
    db.commit()
    from models import KnowledgeChunk

    db.query(KnowledgeChunk).update(
        {"chunk_metadata": None}, synchronize_session=False
    )
    for c in db.query(KnowledgeChunk).all():
        c.chunk_metadata = {"category": "公司内部规范", "status": "proposed"}
    db.commit()

    fixed = migrate_legacy_categories(db, store=memory_store)
    assert fixed >= 2  # document + item 两行

    assert db.get(KnowledgeDocument, doc.id).category == "内部规范"
    item = db.query(KnowledgeItem).filter(KnowledgeItem.document_id == doc.id).first()
    assert item.category == "内部规范"
    for c in db.query(KnowledgeChunk).all():
        assert (c.chunk_metadata or {}).get("category") == "内部规范"
    # 幂等：再次执行为 0
    assert migrate_legacy_categories(db, store=memory_store) == 0


# ══════════════════ 预置知识状态与结构化指标（2026-08 第三轮修复）═══════════════════


def test_upload_with_canonical_status_searchable_without_approve(db, memory_store, fake_embed):
    """status=canonical 的文档（如 seed 预置知识）无需审核即可被默认检索命中。"""
    from models import KnowledgeItem

    svc = _kb(db, memory_store, fake_embed)
    doc = svc.create_from_upload(
        title="预置方法论",
        category="内部规范",
        filename="seed.md",
        raw=METHODOLOGY_MD.encode("utf-8"),
        created_by="seed",
        status="canonical",
    )
    assert doc.status == "canonical"
    item = db.query(KnowledgeItem).filter(KnowledgeItem.document_id == doc.id).first()
    assert item.status == "canonical"
    # 未调用 approve，默认检索（仅 canonical）即可命中
    hits = svc.search("回款风险", top_k=3)
    assert any("回款" in h.content for h in hits)


def test_migrate_seed_knowledge_status(db, memory_store, fake_embed):
    """存量 seed 知识（proposed）被一次性提升为 canonical，切片 metadata 同步。"""
    from models import KnowledgeChunk, KnowledgeDocument, KnowledgeItem
    from services.rag.knowledge_base import migrate_seed_knowledge_status

    svc = _kb(db, memory_store, fake_embed)
    doc = svc.create_from_upload(
        title="预置文档", category="内部规范", filename="m.md", raw=METHODOLOGY_MD.encode("utf-8")
    )
    # 模拟历史 seed 数据：created_by=seed 且状态 proposed
    db.query(KnowledgeDocument).filter(KnowledgeDocument.id == doc.id).update(
        {"created_by": "seed"}, synchronize_session=False
    )
    db.commit()

    assert migrate_seed_knowledge_status(db, store=memory_store) == 1
    assert db.get(KnowledgeDocument, doc.id).status == "canonical"
    item = db.query(KnowledgeItem).filter(KnowledgeItem.document_id == doc.id).first()
    assert item.status == "canonical"
    for c in db.query(KnowledgeChunk).all():
        assert (c.chunk_metadata or {}).get("status") == "canonical"
    # 幂等：再次执行为 0
    assert migrate_seed_knowledge_status(db, store=memory_store) == 0


def test_metrics_crud_and_context_format(app_client, db):
    """结构化指标：写入幂等、按行业精确查询、渲染为 Prompt 段落、删除。"""
    from services.rag.metrics import format_metrics_context, query_metrics

    resp = app_client.post(
        "/api/knowledge/metrics",
        json={
            "metric_key": "industry_avg_score",
            "metric_name": "行业健康度均值",
            "metric_value": 72.5,
            "unit": "分",
            "industry": "金融",
            "period": "2026H1",
        },
    )
    assert resp.status_code == 200, resp.text
    metric_id = resp.json()["id"]

    # 幂等更新：同 key + 行业 + 周期 → 更新而非新增
    resp2 = app_client.post(
        "/api/knowledge/metrics",
        json={
            "metric_key": "industry_avg_score",
            "metric_name": "行业健康度均值",
            "metric_value": 73.0,
            "unit": "分",
            "industry": "金融",
            "period": "2026H1",
        },
    )
    assert resp2.json()["id"] == metric_id
    assert resp2.json()["metric_value"] == 73.0

    rows = query_metrics(db, industry="金融")
    assert len(rows) == 1
    text = format_metrics_context(rows)
    assert "行业基准指标" in text and "73.0分" in text and "金融" in text
    # 其他行业查不到行业特定指标，但能看到通用指标（industry 为空）
    assert query_metrics(db, industry="能源") == []

    # 非法分类被拒
    bad = app_client.post(
        "/api/knowledge/metrics",
        json={"metric_key": "x", "metric_value": 1, "category": "内部规范"},
    )
    assert bad.status_code == 400

    assert app_client.delete(f"/api/knowledge/metrics/{metric_id}").json()["deleted"] is True
    assert query_metrics(db, industry="金融") == []


def test_metrics_injected_into_chat_context(db):
    """客户上下文注入同行业基准指标段落。"""
    from models import Customer
    from services.ai.context_builder import build_context
    from services.rag.metrics import upsert_metric

    c = Customer(customer_name="指标客户", industry="金融")
    db.add(c)
    db.commit()
    upsert_metric(
        db,
        metric_key="industry_avg_score",
        metric_name="行业健康度均值",
        metric_value=72.5,
        unit="分",
        industry="金融",
    )

    ctx = build_context(db, c, query="", retrieve_knowledge=False)
    assert "行业基准指标" in ctx.customer_text
    assert "72.5分" in ctx.customer_text

    # 无匹配行业指标的客户不注入该段落
    c2 = Customer(customer_name="无指标客户", industry="教育")
    db.add(c2)
    db.commit()
    ctx2 = build_context(db, c2, query="", retrieve_knowledge=False)
    assert "行业基准指标" not in ctx2.customer_text

    # 无行业的客户：只注入通用指标，绝不注入其他行业的特定指标
    upsert_metric(
        db,
        metric_key="global_avg_score",
        metric_name="全行业健康度均值",
        metric_value=68.0,
        unit="分",
        industry="",
    )
    c3 = Customer(customer_name="无行业客户", industry="")
    db.add(c3)
    db.commit()
    ctx3 = build_context(db, c3, query="", retrieve_knowledge=False)
    assert "68.0分" in ctx3.customer_text
    assert "72.5分" not in ctx3.customer_text  # 金融行业的特定指标不得错配

    # 有行业的客户：行业特定指标 + 通用指标都可见
    ctx4 = build_context(db, c, query="", retrieve_knowledge=False)
    assert "72.5分" in ctx4.customer_text and "68.0分" in ctx4.customer_text


def test_list_items_tag_search(app_client):
    """标签搜索按标签逐个精确匹配，而非 LIKE 匹配 JSON 序列化文本。"""
    files = {"file": ("m.md", io.BytesIO(METHODOLOGY_MD.encode("utf-8")), "text/markdown")}
    up = app_client.post("/api/knowledge/upload", files=files, data={"category": "内部规范"})
    item_id = up.json()["item_id"]
    app_client.put(f"/api/knowledge/items/{item_id}", json={"tags": ["回款", "政务云"]})

    assert app_client.get("/api/knowledge/items", params={"q": "回款"}).json()["total"] == 1
    assert app_client.get("/api/knowledge/items", params={"q": "政务"}).json()["total"] == 1
    assert app_client.get("/api/knowledge/items", params={"q": "不存在的标签"}).json()["total"] == 0


def test_retrieve_window_expands_neighbors(db, memory_store, fake_embed):
    """窗口扩展：命中切片正文应包含相邻切片，缓解跨切片信息截断。"""
    from models import KnowledgeChunk

    svc = _kb(db, memory_store, fake_embed)
    long_text = "\n".join(
        f"第{i}段：客户满意度评估规则说明，用于制造足够长的切片以便测试窗口扩展。"
        for i in range(60)
    )
    doc = svc.create_from_upload(
        title="窗口扩展测试", category="内部规范", filename="w.md", raw=long_text.encode("utf-8")
    )
    assert doc.chunk_count >= 3

    results = retrieve_knowledge(
        "客户满意度评估规则",
        embed_func=fake_embed,
        store=memory_store,
        db=db,
        top_k=1,
        status=None,
    )
    assert results
    hit = results[0]
    single = (
        db.query(KnowledgeChunk)
        .filter(
            KnowledgeChunk.document_id == hit.document_id,
            KnowledgeChunk.chunk_index == hit.chunk_index,
        )
        .first()
    )
    assert single is not None
    assert len(hit.content) > len(single.content)

    plain = retrieve_knowledge(
        "客户满意度评估规则",
        embed_func=fake_embed,
        store=memory_store,
        db=db,
        top_k=1,
        window=0,
        status=None,
    )
    assert len(plain[0].content) <= len(single.content)


def test_item_list_and_detail_show_chunk_count(app_client):
    """C1：列表 / 详情切片数委托文档 chunk_count，而非恒 0。"""
    files = {"file": ("m.md", io.BytesIO(METHODOLOGY_MD.encode("utf-8")), "text/markdown")}
    up = app_client.post("/api/knowledge/upload", files=files, data={"category": "内部规范"})
    assert up.status_code == 200, up.text
    item_id = up.json()["item_id"]

    lst = app_client.get("/api/knowledge/items").json()
    row = next(x for x in lst["items"] if x["id"] == item_id)
    assert row["chunk_count"] > 0

    detail = app_client.get(f"/api/knowledge/items/{item_id}").json()
    assert detail["chunk_count"] == row["chunk_count"]


def test_upload_industry_metadata_and_edit_syncs_vector(app_client):
    """C3：上传 / 编辑条目行业，切片 metadata 同步（reranker 同行业加权的前提）。"""
    files = {"file": ("m.md", io.BytesIO(METHODOLOGY_MD.encode("utf-8")), "text/markdown")}
    up = app_client.post(
        "/api/knowledge/upload", files=files, data={"category": "内部规范", "industry": "金融"}
    )
    item_id = up.json()["item_id"]
    detail = app_client.get(f"/api/knowledge/items/{item_id}").json()
    assert detail["industry"] == "金融"

    # 编辑行业后向量库 metadata 同步
    upd = app_client.put(f"/api/knowledge/items/{item_id}", json={"industry": "银行"})
    assert upd.status_code == 200
    assert upd.json()["industry"] == "银行"

    import services.rag.vector_store as vs

    store = vs.get_vector_store()
    raw = store.query(_bag_of_chars_embed(["x"])[0], top_k=20)
    hit = [r for r in raw if r["metadata"].get("item_id") == item_id]
    assert hit
    assert all(r["metadata"].get("industry") == "银行" for r in hit)


def test_revoke_and_batch_status_endpoints(app_client):
    """C4：撤销审核 + 批量上下线接口。"""
    files = {"file": ("m.md", io.BytesIO(METHODOLOGY_MD.encode("utf-8")), "text/markdown")}
    up1 = app_client.post("/api/knowledge/upload", files=files, data={"category": "内部规范"})
    up2 = app_client.post("/api/knowledge/upload", files=files, data={"category": "外部指标"})
    id1, id2 = up1.json()["item_id"], up2.json()["item_id"]

    # 批量上线
    r = app_client.post("/api/knowledge/batch-status", json={"ids": [id1, id2], "status": "canonical"})
    assert r.status_code == 200 and r.json()["updated"] == 2
    assert isinstance(r.json().get("warnings"), list)
    assert app_client.get(f"/api/knowledge/items/{id1}").json()["status"] == "canonical"
    assert app_client.get(f"/api/knowledge/items/{id2}").json()["status"] == "canonical"

    # 撤销审核：canonical → proposed
    rv = app_client.post(f"/api/knowledge/items/{id1}/revoke")
    assert rv.status_code == 200 and rv.json()["status"] == "proposed"

    # 批量下线
    r2 = app_client.post("/api/knowledge/batch-status", json={"ids": [id1, id2], "status": "proposed"})
    assert r2.status_code == 200 and r2.json()["updated"] == 2

    # 非法状态 400
    bad = app_client.post("/api/knowledge/batch-status", json={"ids": [id1], "status": "bogus"})
    assert bad.status_code == 400

    # 存在不存在的 id：整体 400，避免静默部分成功
    missing = app_client.post(
        "/api/knowledge/batch-status", json={"ids": [id1, 999_999], "status": "canonical"}
    )
    assert missing.status_code == 400


def test_migrate_add_industry_column_idempotent(db):
    """C3：启动迁移对已有 industry 列的表幂等跳过。"""
    from services.rag.knowledge_base import migrate_add_industry_column

    assert migrate_add_industry_column(db) == 0  # 新表 create_all 已含列


def test_migrate_add_industry_column_adds_to_legacy_table(tmp_path):
    """旧库缺列时 ALTER 成功，且再次执行幂等（覆盖真实升级路径）。"""
    from sqlalchemy import create_engine, text as sa_text
    from sqlalchemy.orm import sessionmaker

    from services.rag.knowledge_base import migrate_add_industry_column

    db_path = tmp_path / "legacy.db"
    eng = create_engine(f"sqlite:///{db_path}")
    with eng.begin() as conn:
        conn.execute(
            sa_text(
                "CREATE TABLE knowledge_items ("
                "id INTEGER PRIMARY KEY, document_id INTEGER, title TEXT, category TEXT, "
                "tags TEXT, summary TEXT, storage TEXT, status TEXT, "
                "adoption_count INTEGER DEFAULT 0, hit_count INTEGER DEFAULT 0, "
                "created_by TEXT, created_at DATETIME, updated_at DATETIME)"
            )
        )
    Session = sessionmaker(bind=eng)
    db = Session()
    try:
        assert migrate_add_industry_column(db) == 1
        cols = {r[1] for r in db.execute(sa_text("PRAGMA table_info(knowledge_items)"))}
        assert "industry" in cols
        assert migrate_add_industry_column(db) == 0
    finally:
        db.close()
        eng.dispose()


def test_startup_runs_sql_schema_migration_when_chroma_is_unavailable(
    tmp_path, monkeypatch
):
    """Chroma fail-closed 不能阻断 knowledge_items 的纯 SQL 补列。"""
    from sqlalchemy import text as sa_text
    from sqlalchemy.orm import sessionmaker
    import main

    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy-knowledge.db'}")
    with legacy_engine.begin() as connection:
        connection.execute(sa_text(
            "CREATE TABLE knowledge_items ("
            "id INTEGER PRIMARY KEY, document_id INTEGER, title VARCHAR(200), "
            "category VARCHAR(30), status VARCHAR(20))"
        ))
    monkeypatch.setattr(main, "SessionLocal", sessionmaker(bind=legacy_engine))
    monkeypatch.setattr(
        "services.rag.vector_store.get_vector_store",
        lambda: (_ for _ in ()).throw(RuntimeError("chroma down")),
    )
    main._migrate_required_schema()
    main._migrate_legacy_data()
    with legacy_engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.execute(sa_text("PRAGMA table_info(knowledge_items)"))
        }
    assert "industry" in columns


def test_retrieval_defers_writes_until_commit(tmp_path, memory_store, fake_embed):
    """检索与模型生成之间，另一个连接仍能写入；统计不能提交调用方的更改。"""
    eng = create_engine(f"sqlite:///{tmp_path / 'concurrent-hits.db'}")
    Base.metadata.create_all(eng)
    factory = sessionmaker(bind=eng, autoflush=False)
    try:
        with factory() as db:
            svc = _kb(db, memory_store, fake_embed)
            doc = svc.create_from_upload(title="并发规范", category="内部规范",
                                         filename="rules.md", raw=METHODOLOGY_MD.encode())
            item = db.query(KnowledgeItem).filter_by(document_id=doc.id).one()
            svc.approve_item(item.id)
            item_id = item.id
            item.summary = "未提交的更改"
            for _ in range(2):
                assert retrieve_knowledge("回款", db=db, store=memory_store, embed_func=fake_embed)
            # 若检索执行了 UPDATE，这里的独立写入会报 database is locked。
            with factory() as other:
                other.add(Customer(customer_name="并发新增客户"))
                other.commit()
                stored = other.get(KnowledgeItem, item_id)
                assert stored.hit_count == 0
                assert stored.summary != "未提交的更改"
            db.commit()
            with factory() as other:
                stored = other.get(KnowledgeItem, item_id)
                assert stored.hit_count == 2
                assert stored.summary == "未提交的更改"
            assert retrieve_knowledge("回款", db=db, store=memory_store, embed_func=fake_embed)
            db.rollback()
            db.commit()
            assert db.get(KnowledgeItem, item_id).hit_count == 2
            assert retrieve_knowledge("回款", db=db, store=memory_store, embed_func=fake_embed)
            db.close()
            db.commit()
            assert db.get(KnowledgeItem, item_id).hit_count == 2
    finally:
        eng.dispose()


def test_pdf_generation_and_check_receive_full_retrieved_source(db, memory_store, fake_embed, monkeypatch):
    from services.ai import llm_adapter
    from services.report_builder import build_report_data
    from test_fact_check import ScriptedAdapter

    full = "回款核验必须保留记录。" * 24 + "必须在37小时内完成核验，在73小时内取得付款排期。"
    svc = _kb(db, memory_store, fake_embed)
    doc = svc.create_from_upload(title="回款规则", category="内部规范", filename="full.md", raw=full.encode())
    item = db.query(KnowledgeItem).filter_by(document_id=doc.id).one()
    svc.approve_item(item.id)
    customer = Customer(customer_name="测试客户")
    db.add(customer)
    db.commit()
    monkeypatch.setattr("services.rag.retriever.make_embedding_func", lambda: fake_embed)
    monkeypatch.setattr("services.rag.retriever.get_vector_store", lambda: memory_store)
    adapter = ScriptedAdapter(['应在37小时内完成核验。参考：回款规则。', '{"issues":[]}'])
    monkeypatch.setattr(llm_adapter, "get_chat_adapter", lambda: adapter)
    report = build_report_data(db, customer, scenario="assessment")
    assert len(adapter.calls) == 2  # 首次生成 + 独立核对，无修订
    for messages, _ in adapter.calls:
        assert "37小时" in messages[-1].content
        assert "73小时" in messages[-1].content
    assert report.timings["verification"][0]["status"] == "passed"
    assert "37小时" not in report.references[0]["snippet"]
    assert all("_model_content" not in ref for ref in report.references)


def test_rag_redaction_preserves_business_sentence_after_credential(db, memory_store, fake_embed):
    svc = _kb(db, memory_store, fake_embed)
    raw = '内部升级口令是“demo-472”。\nP1中断必须45分钟内响应，由交付经理牵头。'
    doc = svc.create_from_upload(title="响应规则", category="内部规范", filename="secret.md", raw=raw.encode())
    item = db.query(KnowledgeItem).filter_by(document_id=doc.id).one()
    svc.approve_item(item.id)
    results = retrieve_knowledge("P1响应", db=db, store=memory_store, embed_func=fake_embed)
    assert results and "demo-472" not in results[0].content
    assert "P1中断必须45分钟内响应" in results[0].content
