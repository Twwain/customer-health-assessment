"""Agent 工具集。

M4 Agent Loop 调用的四类工具：
- ``rag_retrieve``：知识库检索（检索增强上下文，Agent 自带 RAG 能力）
- ``score_query``：量化评估引擎查询（基础客情分）
- ``profile_query``：客户画像查询（行业 / 规模 / 回款等）
- ``critique``：自批判（启发式，检查草稿是否含策略块与知识引用；可升级为 LLM 批判）

所有工具对异常静默降级（返回空 / 默认），不让工具故障拖垮 Agent Loop。
"""

from __future__ import annotations

import json
from typing import Any

from models import Customer
from services.ai.context_builder import ChatContext
from services.rag.retriever import RetrievedChunk, retrieve_knowledge

# 工具循环安全上限：模型连续请求工具的最大轮数，防止死循环
MAX_TOOL_ROUNDS = 5
# 工具返回的单条知识正文最大字符数：防止全文 + 窗口膨胀把上下文撑爆
TOOL_CONTENT_MAX_CHARS = 800


def rag_retrieve(
    query: str,
    customer: Customer | None = None,
    db: Any | None = None,
    *,
    k: int = 5,
    embed_func=None,
    store=None,
    timings: dict[str, int] | None = None,
) -> list[RetrievedChunk]:
    """检索知识库，返回命中切片。空查询或异常时返回 []。"""
    if not query or not query.strip():
        return []
    try:
        return retrieve_knowledge(
            query, customer=customer, top_k=k, db=db, embed_func=embed_func,
            store=store, timings=timings,
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
        "snippet": (chunk.hit_content or chunk.content)[:200],
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
    """客户基础画像；风险判断统一通过 score_query 的新版因子预警获取。"""
    if not customer:
        return {}
    return {
        "customer_name": customer.customer_name,
        "industry": customer.industry or "",
        "cooperation_years": customer.cooperation_years,
        "contract_amount": customer.contract_amount,
        "growth_potential": customer.growth_potential or "",
    }


# ── 客户对比工具（真 function calling）─────────────────────────────────────

CUSTOMER_COMPARE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "customer_compare",
        "description": (
            "查询客户库中客户的量化健康评分与四维得分，用于横向对比（例如"
            "“和其他客户对比有什么优势”“和同行业客户比怎么样”）。"
            "默认返回全量客户；仅当用户明确限定行业（如同行业/同行/某行业）时传 industry。"
            "exclude_customer_id 通常传当前对话客户 id，避免把目标客户自身计入对比。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "industry": {
                    "type": "string",
                    "description": "行业过滤（如：能源、金融）。不传则查询全量客户。",
                },
                "exclude_customer_id": {
                    "type": "integer",
                    "description": "需要排除的客户 id（通常为当前对话客户）。",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回多少条对比结果，默认 30，最大 100。",
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "additionalProperties": False,
        },
    },
}

TOOL_HANDLERS: dict[str, Any] = {}


def customer_compare(
    customer: Customer | None = None,
    db: Any | None = None,
    *,
    industry: str | None = None,
    exclude_customer_id: int | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    """从客户库查询客户评分摘要，供模型做横向对比。

    - 默认返回全量客户；传 ``industry`` 时按行业过滤
    - ``exclude_customer_id`` 用于排除当前对话客户自身
    - 评分为实时计算（与评估引擎一致），含客情评分 / 等级 / 分维度得分 / 均值
    """
    # 单租户内部工具：全量客户库对当前会话可见。若未来支持多租户，
    # 必须在此按租户范围过滤，避免把其他租户客户数据注入 prompt。
    if db is None:
        return {"scope": "all", "count": 0, "avg_score": None, "customers": []}

    from services.scoring import get_scoring_strategy  # 延迟导入，避免循环依赖

    query = db.query(Customer)
    if industry:
        query = query.filter(Customer.industry == industry)
    if exclude_customer_id is not None:
        query = query.filter(Customer.id != exclude_customer_id)
    rows = query.order_by(Customer.id).limit(max(1, limit)).all()

    engine = get_scoring_strategy()
    customer_rows: list[dict[str, Any]] = []
    for c in rows:
        try:
            assessment = engine.evaluate(c)
        except Exception:  # pragma: no cover - 单个客户评分失败不阻断对比
            continue
        customer_rows.append(
            {
                "id": c.id,
                "name": c.customer_name,
                "industry": c.industry or "",
                "total_score": round(assessment.total_score, 1),
                "level": assessment.level,
                "dimensions": {d.name: round(d.score, 1) for d in assessment.dimensions},
            }
        )

    scores = [row["total_score"] for row in customer_rows]
    return {
        "scope": industry or "all",
        "count": len(customer_rows),
        "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
        "customers": customer_rows,
    }


KNOWLEDGE_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "knowledge_search",
        "description": (
            "检索知识库中的参考资料（内部规范、内部指标基准、外部指标、对话沉淀等）。"
            "当预置上下文中的知识不足、需要更多细节、或需要按分类检索时调用；"
            "返回命中条目与来源（正文按约 800 字符截断），供作答时引用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索词或问题，使用用户问题的核心内容",
                },
                "category": {
                    "type": "string",
                    "description": "知识分类过滤（可选）：内部规范 / 内部指标 / 外部指标 / 对话沉淀",
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回条数（可选，默认 8，最大 15）",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


def knowledge_search(
    customer: Customer | None = None,
    db: Any | None = None,
    *,
    query: str = "",
    category: str | None = None,
    top_k: int = 8,
) -> dict[str, Any]:
    """检索知识库（含窗口扩展），返回命中条目供模型引用。"""
    if not query or not query.strip():
        return {"error": "query 不能为空"}
    from services.rag.retriever import retrieve_knowledge

    try:
        k = min(max(int(top_k or 8), 1), 15)
    except (TypeError, ValueError):
        k = 8
    try:
        chunks = retrieve_knowledge(
            query, customer=customer, category=category or None, top_k=k, db=db
        )
    except Exception as exc:  # pragma: no cover - 检索故障不中断对话
        return {"error": f"知识检索失败：{exc}"}
    return {
        "count": len(chunks),
        "results": [
            {
                "id": f"{c.document_id}:{c.chunk_index}",
                "title": c.item_title,
                "category": c.category,
                "score": round(c.score, 4),
                "snippet": (c.hit_content or c.content)[:200],
                "content": c.content[:TOOL_CONTENT_MAX_CHARS],
                "document_id": c.document_id,
                "chunk_index": c.chunk_index,
                "item_id": c.item_id,
            }
            for c in chunks
        ],
    }


def execute_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    customer: Customer | None = None,
    db: Any | None = None,
) -> dict[str, Any]:
    """执行函数调用工具；失败返回 error 字段，不让工具故障中断对话。"""
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return {"error": f"未知工具：{name}"}
    try:
        result = handler(customer=customer, db=db, **arguments)
        return result if isinstance(result, dict) else {"result": result}
    except Exception as exc:  # pragma: no cover - 工具故障静默降级
        return {"error": f"工具执行失败：{exc}"}


TOOL_HANDLERS["customer_compare"] = customer_compare
TOOL_HANDLERS["knowledge_search"] = knowledge_search
TOOL_SCHEMAS: list[dict[str, Any]] = [CUSTOMER_COMPARE_SCHEMA, KNOWLEDGE_SEARCH_SCHEMA]


def _parse_arguments(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):  # pragma: no cover - 模型偶发非法 JSON
        return {}


def append_tool_results(
    messages: list[Any],
    tool_calls: list[dict[str, Any]],
    *,
    customer: Customer | None = None,
    db: Any | None = None,
    exclude_ids: set[str] | None = None,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """把 assistant 的 tool_calls 与工具执行结果回填到消息列表。

    返回 ``(消息列表, 工具引用列表)``；``exclude_ids`` 用于对 knowledge_search
    结果去重（通常是预取上下文中已有的 chunk id），避免重复注入。
    """
    assistant_msg: dict[str, Any] = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": tc.get("id") or "",
                "type": "function",
                "function": {
                    "name": tc.get("name") or "",
                    "arguments": tc.get("arguments") or "{}",
                },
            }
            for tc in tool_calls
        ],
    }
    messages = [*messages, assistant_msg]
    references: list[dict[str, Any]] = []
    for tc in tool_calls:
        name = tc.get("name") or ""
        result = execute_tool(
            name,
            _parse_arguments(tc.get("arguments")),
            customer=customer,
            db=db,
        )
        if name == "knowledge_search" and isinstance(result, dict):
            results = result.get("results") or []
            if exclude_ids:
                results = [r for r in results if r.get("id") not in exclude_ids]
                result = {**result, "count": len(results), "results": results}
            for r in results:
                references.append(
                    {
                        "id": r.get("id") or "",
                        "title": r.get("title") or "",
                        "category": r.get("category") or "",
                        "score": r.get("score") or 0.0,
                        "snippet": (r.get("snippet") or r.get("content") or "")[:200],
                        "chunk_id": r.get("chunk_index"),
                        "document_id": r.get("document_id"),
                        "item_id": r.get("item_id"),
                    }
                )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tc.get("id") or "",
                "content": json.dumps(result, ensure_ascii=False, default=str),
            }
        )
    return messages, references


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
