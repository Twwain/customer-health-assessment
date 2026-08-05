"""Agent 工具集（SOW §4.2 services/ai/tools.py）。

M4 Agent Loop 调用的四类工具：
- ``rag_retrieve``：知识库检索（检索增强上下文，Agent 自带 RAG 能力）
- ``score_query``：量化评估引擎查询（基础客情分）
- ``profile_query``：客户画像查询（行业 / 规模 / 回款等）
- ``critique``：自批判（启发式，检查草稿是否含策略块与知识引用；可升级为 LLM 批判）

所有工具对异常静默降级（返回空 / 默认），不让工具故障拖垮 Agent Loop。
"""

from __future__ import annotations

from typing import Any

from models import Customer
from services.ai.context_builder import ChatContext
from services.rag.retriever import RetrievedChunk, retrieve_knowledge


def rag_retrieve(
    query: str,
    customer: Customer | None = None,
    db: Any | None = None,
    *,
    k: int = 5,
    embed_func=None,
    store=None,
) -> list[RetrievedChunk]:
    """检索知识库，返回命中切片。空查询或异常时返回 []。"""
    if not query or not query.strip():
        return []
    try:
        return retrieve_knowledge(
            query, customer=customer, top_k=k, db=db, embed_func=embed_func, store=store
        )
    except Exception:  # pragma: no cover - 检索故障不影响 Agent
        return []


def to_reference(chunk: RetrievedChunk) -> dict:
    """把检索命中转成前端 📎 溯源抽屉可用的引用结构。"""
    return {
        "id": f"{chunk.document_id}:{chunk.chunk_index}",
        "title": chunk.item_title,
        "category": chunk.category,
        "score": round(chunk.score, 4),
        "snippet": chunk.content[:200],
        "chunk_id": chunk.chunk_index,
        "document_id": chunk.document_id,
        "item_id": chunk.item_id,
    }


def score_query(customer: Customer | None, db: Any | None = None):
    """查询量化评估引擎输出的基础客情分。"""
    if not customer:
        return None
    from services.scoring import get_scoring_strategy

    return get_scoring_strategy().evaluate(customer)


def profile_query(customer: Customer | None) -> dict:
    """客户画像（行业 / 合作年限 / 回款 / 竞品等），供 Agent 判断上下文。"""
    if not customer:
        return {}
    return {
        "customer_name": customer.customer_name,
        "industry": customer.industry or "",
        "cooperation_years": customer.cooperation_years,
        "contract_amount": customer.contract_amount,
        "payment_status": customer.payment_status or "",
        "competitor_involvement": bool(customer.competitor_involvement),
        "growth_potential": customer.growth_potential or "",
    }


def critique(
    draft: str,
    ctx: ChatContext | None = None,
    *,
    scenario: str = "strategy",
    has_knowledge: bool = True,
) -> str:
    """启发式自批判：草稿不满足场景要求时返回改进建议，否则返回空串。

    检查项按场景裁剪，避免"永远触发 refine"：
    - ```json 策略块：仅 strategy 场景要求（assessment / alert_analysis 模板输出纯 Markdown）
    - 知识引用：仅当本轮检索确实有命中时才要求（知识库为空时模型无从引用）

    返回非空即表示需要精炼（refine）。后续可升级为 LLM 批判以更细腻地判断质量。
    """
    if not draft or not draft.strip():
        return "草稿为空，请重新生成评估结论与策略建议。"
    missing = []
    if scenario == "strategy" and "```json" not in draft:
        missing.append("```json 策略块（priority/title/urgency/...）")
    if has_knowledge:
        has_ref = ("参考" in draft) or ("reference" in draft.lower()) or ("📎" in draft)
        if not has_ref:
            missing.append("知识库引用（📎 参考，指向命中切片）")
    if not missing:
        return ""
    return "请在结论中补充：" + "、".join(missing) + "。"


def needs_refine(critique_text: str) -> bool:
    return bool(critique_text and critique_text.strip())
