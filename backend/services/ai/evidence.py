"""Freeze SQL-authoritative evidence; distinguish no hits from unavailable."""
from models import KnowledgeChunk, KnowledgeDocument, KnowledgeItem
from services.rag.retriever import KnowledgeRetriever
from services.ai.guardrails import sanitize_for_outbound
from .protocol import fingerprint, ProtocolError
from .policy_scope import normalize_evidence
from services.ai.llm_adapter import get_embedding_usage


def retrieve(db, package, question, customer):
    timings = {}
    try:
        chunks = KnowledgeRetriever().retrieve(sanitize_for_outbound(question)[0], customer=customer,
            db=db, window=0, timings=timings, strict=True)
    except ProtocolError:
        package["retrieval_usage"] = get_embedding_usage()
        raise
    except Exception as exc:
        package["retrieval_usage"] = get_embedding_usage()
        package["retrieval_status"] = "unavailable"
        raise ProtocolError("dependency_unavailable") from exc
    evidence = []
    package["retrieval_usage"] = get_embedding_usage()
    for hit in chunks:
        row = db.query(KnowledgeChunk).filter_by(document_id=hit.document_id, chunk_index=hit.chunk_index).first()
        doc, item = db.get(KnowledgeDocument, hit.document_id), db.get(KnowledgeItem, hit.item_id)
        if not row or not doc or not item or item.document_id != doc.id or doc.status != "canonical" or item.status != "canonical" or doc.index_status != "indexed":
            raise ProtocolError("source_changed")
        evidence.append(dict(chunk_id=row.id, document_id=doc.id, item_id=item.id, title=item.title,
            category=item.category, content=row.content, source_class="general_knowledge",
            applicability="unknown", content_hash=fingerprint(dict(content=row.content, title=item.title,
                category=item.category, status=item.status, document_status=doc.status))))
    evidence = normalize_evidence(evidence)
    package["evidence"] = evidence
    package["snapshot"]["evidence_hashes"] = [e["content_hash"] for e in evidence]
    for e in evidence:
        ref = "src:"+str(e["chunk_id"])
        package["sources"][ref] = dict(id=ref, kind="knowledge", record_ref="knowledge:"+str(e["chunk_id"]),
            content_hash=e["content_hash"], visibility_state="active", source_class="general_knowledge")
    package["retrieval_status"] = "available" if evidence else "no_hits"
    package["input_fingerprint"] = package["source_fingerprint"]
    package["source_fingerprint"] = fingerprint([package["input_fingerprint"], [
        dict(document_id=e["document_id"],item_id=e["item_id"],chunk_id=e["chunk_id"],
             content_hash=e["content_hash"]) for e in evidence]])
    return timings
