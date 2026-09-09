"""低频在线验收：真实 Embedding，不调用 Chat，不使用业务数据目录。"""

import json
import math
from pathlib import Path
import subprocess
import sys
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from database import Base
from models import KnowledgeItem
from services.rag import knowledge_base
from services.rag.reranker import MetadataReranker
from services.rag.retriever import KnowledgeRetriever
from services.rag.vector_store import ChromaVectorStore, InMemoryVectorStore
from test_live_llm import isolated_adapters, live_embed, record

pytestmark = pytest.mark.live


@pytest.mark.live_chroma
def test_live_chroma_lifecycle(live_embed, tmp_path, monkeypatch, record_property):
    monkeypatch.setattr(knowledge_base, "KNOWLEDGE_DATA_DIR", str(tmp_path / "uploads"))
    persist = tmp_path / "chroma"
    engine = create_engine(f"sqlite:///{(tmp_path / 'knowledge.db').as_posix()}")
    Base.metadata.create_all(engine)
    started = time.monotonic()
    try:
        with Session(engine) as db:
            store = ChromaVectorStore(persist_dir=str(persist), collection="live_lifecycle")
            kb = knowledge_base.KnowledgeBaseService(db, store=store, embed_func=live_embed)
            doc = kb.create_from_upload(
                title="临时续约制度", category="内部规范", filename="renewal.txt",
                raw="合同到期前60天，由客户经理启动续签沟通。".encode(), status="canonical",
            )
            assert doc.index_status == "indexed" and store.count() > 0
            item = db.query(KnowledgeItem).filter_by(document_id=doc.id).one()
            query = "合约即将结束，提前多久联系客户继续合作？"
            query_vector = live_embed([query])[0]
            vector_file = tmp_path / "query-vector.json"
            vector_file.write_text(json.dumps(query_vector), encoding="utf-8")
            # 独立进程打开磁盘索引，排除同进程 Chroma 客户端缓存导致的伪持久化验证。
            child = subprocess.run(
                [sys.executable, "-c", """
import json, sys
from pathlib import Path
from services.rag.vector_store import ChromaVectorStore
store = ChromaVectorStore(persist_dir=sys.argv[1], collection='live_lifecycle')
vector = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
hits = store.query(vector, top_k=1, where={'status': 'canonical'})
assert hits and hits[0]['metadata']['document_id'] == int(sys.argv[3])
print('PERSISTENCE_OK')
""", str(persist), str(vector_file), str(doc.id)],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=60,
            )
            assert child.returncode == 0, child.stderr
            assert "PERSISTENCE_OK" in child.stdout
            reopened = ChromaVectorStore(persist_dir=str(persist), collection="live_lifecycle")
            kb = knowledge_base.KnowledgeBaseService(db, store=reopened, embed_func=live_embed)
            retriever = KnowledgeRetriever(store=reopened, embed_func=live_embed, reranker=MetadataReranker())
            assert retriever.retrieve(query, db=db, top_k=1)[0].document_id == doc.id
            assert not kb.revoke_item(item.id).sync_pending
            assert retriever.retrieve(query, db=db) == []
            assert not reopened.query(query_vector, top_k=1, where={"status": "canonical"})
            assert not kb.approve_item(item.id).sync_pending
            assert retriever.retrieve(query, db=db, top_k=1)[0].document_id == doc.id
            uploaded_path = Path(doc.file_path)
            assert kb.delete_item(item.id)
            assert reopened.count() == 0
            assert retriever.retrieve(query, db=db) == []
            assert not uploaded_path.exists()
            record(record_property, chroma_lifecycle_seconds=time.monotonic() - started)
    finally:
        engine.dispose()


# 固定标注集：相近业务概念作为干扰项；每个问题只对应一项制度。
DOCUMENTS = [
    (1, "逾期回款", "客户款项逾期，财务负责人36小时内核验单据，72小时内取得付款排期。"),
    (2, "合同续签", "客户合同到期前60天，由客户经理启动续签沟通。"),
    (3, "数据备份", "服务器数据每周日凌晨备份，数据副本保留14天。"),
    (4, "客户投诉", "客户投诉由服务主管4小时内响应，2个工作日内提交解决方案。"),
    (5, "发票开具", "财务收到客户开票资料后3个工作日内开具发票。"),
    (6, "合同终止", "客户提出终止合作，由法务在5个工作日内审核解约条款。"),
    (7, "账号停用", "员工离职当天由信息部门注销系统账号。"),
    (8, "故障恢复", "核心系统中断后，运维团队须在2小时内恢复服务。"),
    (9, "客户访谈", "客户经理每季度开展一次满意度访谈，记录改进建议。"),
    (10, "折扣审批", "销售报价折扣超过百分之十五时，必须取得销售总监批准。"),
    (11, "资料保密", "客户商业资料只能在授权项目组内共享，对外披露前取得书面许可。"),
    (12, "退款办理", "客户退款申请通过审核后，财务在7个工作日内退回款项。"),
]
QUESTIONS = [
    ("客户欠款没付，谁来核实凭证和跟进支付安排？", 1),
    ("催收流程里核验账单与拿到付款计划的时限分别是多少？", 1),
    ("合约快到期了，提前多久商谈继续合作？", 2),
    ("续约工作由谁负责，在到期前几天开始？", 2),
    ("系统数据副本可以保存几个星期？", 3),
    ("每周什么时候做服务器数据备份？", 3),
    ("客户表达不满后，多快需要回应并提出处理办法？", 4),
    ("开票信息齐全后多久能拿到发票？", 5),
    ("客户不想继续合作，谁审查解除合同的约定？", 6),
    ("同事离开公司后，登录权限什么时候收回？", 7),
    ("关键业务系统宕机，恢复时限是什么？", 8),
    ("多久向客户调查一次服务满意程度？", 9),
    ("报价让利幅度大于15%，找谁签字？", 10),
    ("准备把客户商务文件交给外部人员，要先做什么？", 11),
    ("退钱审批完成后，客户多久能收到款项？", 12),
    ("服务中断时的抢修责任团队是哪个？", 8),
    ("客户投诉的处理方案要在几个工作日内给出？", 4),
    ("客户申请退还资金的到账办理期限是多少？", 12),
]


@pytest.mark.live_eval
def test_live_retrieval_quality_and_latency(live_embed, request, tmp_path, record_property):
    repeats = request.config.getoption("--live-eval-repeats")
    p95_limit = request.config.getoption("--live-eval-p95-seconds")
    assert repeats >= 1 and math.isfinite(p95_limit) and p95_limit > 0
    store = InMemoryVectorStore()
    texts = [text for _, _, text in DOCUMENTS]
    store.add([f"{i}:0" for i, _, _ in DOCUMENTS], texts, live_embed(texts),
              [{"document_id": i, "item_id": i, "chunk_index": 0, "title": title,
                "category": "内部规范", "status": "canonical"} for i, title, _ in DOCUMENTS])
    retriever = KnowledgeRetriever(store=store, embed_func=live_embed, reranker=MetadataReranker())
    rows = []
    # 不缓存查询向量，每个样本都走一次真实 Embedding；不并发压测。
    for repeat in range(repeats):
        for question, expected in QUESTIONS:
            started = time.monotonic()
            hits = retriever.retrieve(question, top_k=3, window=0)
            elapsed = time.monotonic() - started
            ids = [hit.document_id for hit in hits]
            rank = ids.index(expected) + 1 if expected in ids else None
            rows.append({"repeat": repeat, "question": question, "expected": expected,
                         "actual": ids, "rank": rank, "seconds": elapsed})
    count = len(rows)
    latencies = sorted(row["seconds"] for row in rows)
    metrics = {
        "samples": count, "unique_questions": len(QUESTIONS), "repeats": repeats,
        "recall_at_1": sum(row["rank"] == 1 for row in rows) / count,
        "recall_at_3": sum(row["rank"] is not None for row in rows) / count,
        "mrr_at_3": sum(1 / row["rank"] if row["rank"] else 0 for row in rows) / count,
        "p50_seconds": latencies[math.ceil(count * .50) - 1],
        "p95_seconds": latencies[math.ceil(count * .95) - 1],
        "max_seconds": latencies[-1],
    }
    artifact = tmp_path / "retrieval-evaluation.json"
    artifact.write_text(json.dumps({"metrics": metrics, "samples": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    record(record_property, **metrics, evaluation_artifact=str(artifact))
    assert metrics["recall_at_1"] >= .85, "Top-1 召回率低于固定验收线，详见逐题记录"
    assert metrics["recall_at_3"] >= .95, "Top-3 召回率低于固定验收线，详见逐题记录"
    assert metrics["p95_seconds"] <= p95_limit, "检索 P95 超出设定上限"
