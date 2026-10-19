"""真实接口冒烟测试：显式 --run-live，数据均为合成，不使用业务库。"""

import json
import math
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import config
from database import Base
from models import ChatMessage
from services.ai import chat_engine, llm_adapter
from services.ai.llm_adapter import LLMMessage
from services.rag import vector_store
from services.rag.embeddings import make_embedding_func
from services.rag.reranker import MetadataReranker
from services.rag.retriever import KnowledgeRetriever
from services.rag.vector_store import InMemoryVectorStore

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def isolated_adapters(monkeypatch):
    # 恢复单例，避免在线与离线测试相互污染；限制失败重试成本。
    monkeypatch.setattr(llm_adapter, "_chat_adapter", None)
    monkeypatch.setattr(llm_adapter, "_embedding_adapter", None)
    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 0)


@pytest.fixture
def live_chat():
    missing = [name for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL") if not getattr(config, name)]
    if missing or not config.LLM_ENABLED or config.LLM_API_KEY.startswith("enc:"):
        pytest.fail("在线 Chat 配置缺失、未解密或未启用：检查 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL / LLM_ENABLED", pytrace=False)
    return llm_adapter.get_chat_adapter()


@pytest.fixture
def live_embed():
    missing = [name for name in ("EMBEDDING_API_KEY", "EMBEDDING_BASE_URL", "EMBEDDING_MODEL") if not getattr(config, name)]
    if missing or not config.EMBEDDING_ENABLED or config.EMBEDDING_API_KEY.startswith("enc:"):
        pytest.fail("在线 Embedding 配置缺失、未解密或未启用：检查 LLM_EMBEDDING_* / EMBEDDING_ENABLED", pytrace=False)
    return make_embedding_func()


@pytest.fixture
def limits(request):
    first = request.config.getoption("--live-first-token-seconds")
    total = request.config.getoption("--live-total-seconds")
    assert math.isfinite(first) and math.isfinite(total) and 0 < first <= total
    return first, total


def record(record_property, **metrics):
    for name, value in metrics.items():
        record_property(name, value)
    print("LIVE_METRICS " + json.dumps(metrics, ensure_ascii=False))


def test_live_stream_latency(live_chat, limits, record_property):
    first_limit, total_limit = limits
    started = time.monotonic()
    first = None
    chunks = []
    for chunk in live_chat.stream_chat_completion(
        [LLMMessage("user", "请用一句中文解释客户健康度。")],
        max_tokens=128, extra={"thinking": {"type": "disabled"}},
        deadline=started + total_limit,
    ):
        if chunk:
            if first is None:
                first = time.monotonic() - started
            chunks.append(chunk)
    elapsed = time.monotonic() - started
    record(record_property, first_token_seconds=first, total_seconds=elapsed)
    assert "".join(chunks).strip(), "真实模型未返回正文"
    assert first is not None and first <= first_limit
    assert elapsed <= total_limit


def test_live_session_turn(live_chat, limits, monkeypatch, record_property):
    # 此项只测会话，不要求 Embedding Key；RAG 在独立用例验证。
    monkeypatch.setattr(config, "EMBEDDING_API_KEY", "")
    monkeypatch.setattr(config, "KNOWLEDGE_VECTOR_STORE", "memory")
    monkeypatch.setattr(vector_store, "_MEMORY_STORE", InMemoryVectorStore())
    monkeypatch.setattr(config, "LLM_TOOLS_ENABLED", False)
    monkeypatch.setattr(config, "LLM_MAX_TOKENS", 256)
    monkeypatch.setattr(config, "CHAT_TURN_TIMEOUT_SECONDS", limits[1])
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            session = chat_engine.create_session(db, scenario="free_qa")
            events = list(chat_engine.run_turn(db, session, content="请用一句话说明为什么要关注客户续约。"))
            assert not any(event.type in {"error", "warning"} for event in events), "会话发生错误或降级警告"
            assert events[-1].type == "done"
            done = events[-1].data
            assert done["degraded"] is False, "规则引擎降级不能算在线测试通过"
            assert done["message"]["content"].strip()
            timings = done["timings"]
            record(record_property, session_total_ms=timings["total_ms"], session_first_delta_ms=timings["time_to_first_client_delta_ms"])
            assert timings["llm_call_count"] >= 1
            assert 0 <= timings["time_to_first_client_delta_ms"] <= limits[0] * 1000
            assert timings["total_ms"] <= limits[1] * 1000
            assert db.query(ChatMessage).filter_by(session_id=session.id, role="assistant").count() == 1
    finally:
        engine.dispose()


@pytest.fixture
def live_retriever(live_embed):
    texts = [
        "回款制度：客户账款超过付款期限，应由财务负责人在36小时内联系客户确认付款计划。",
        "续约制度：合同到期前60天，由客户经理启动续签沟通。",
        "设备制度：服务器每周日凌晨进行备份，备份保留14天。",
        "未审核回款草稿：逾期款项一律等待999天再处理。",
    ]
    vectors = live_embed(texts)
    assert len(vectors) == len(texts)
    assert all(len(v) == config.EMBEDDING_DIM and all(math.isfinite(x) for x in v) and any(v) for v in vectors)
    store = InMemoryVectorStore()
    store.add(
        [f"{i}:0" for i in range(1, 5)], texts, vectors,
        [{"document_id": i, "item_id": i, "chunk_index": 0, "title": f"合成制度{i}",
          "category": "内部规范", "status": "canonical" if i < 4 else "proposed"} for i in range(1, 5)],
    )
    return KnowledgeRetriever(store=store, embed_func=live_embed, reranker=MetadataReranker())


@pytest.mark.parametrize("question,expected", [
    ("客户一直拖欠货款，谁应该在多久之内跟进？", 1),
    ("合约快到期了，提前多久开始商谈继续合作？", 2),
    ("系统数据副本能保存几个星期？", 3),
])
def test_live_rag_retrieval(live_retriever, question, expected, limits, record_property):
    started = time.monotonic()
    timings = {}
    hits = live_retriever.retrieve(question, top_k=1, window=0, timings=timings)
    elapsed = time.monotonic() - started
    record(record_property, retrieval_seconds=elapsed, **timings)
    assert hits, "真实 Embedding 检索失败或降级为空"
    assert hits[0].document_id == expected, "语义检索未命中预期知识"
    assert hits[0].metadata["status"] == "canonical"
    assert elapsed <= limits[1]


def test_live_rag_grounded_answer(live_chat, live_retriever):
    hits = live_retriever.retrieve("账款逾期由谁处理，时限是多少？", top_k=1, window=0)
    assert hits and hits[0].document_id == 1
    result = live_chat.chat_completion(
        [LLMMessage("system", '仅依据给定资料回答，输出纯JSON，字段为owner、hours（整数）、source_id（整数）。'),
         LLMMessage("user", f"资料 source_id={hits[0].document_id}：{hits[0].content}\n问题：账款逾期由谁处理，时限是多少？")],
        max_tokens=256, temperature=0, extra={"thinking": {"type": "disabled"}},
    )
    answer = json.loads(result.content)
    assert answer["hours"] == 36
    assert "财务" in answer["owner"]
    assert answer["source_id"] == 1


def test_live_tool_call(live_chat):
    result = live_chat.chat_completion(
        [LLMMessage("user", "必须调用knowledge_search工具查询回款制度，不要直接回答。")],
        tools=[{"type": "function", "function": {"name": "knowledge_search", "description": "查询知识库",
               "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}],
        max_tokens=256, temperature=0,
        extra={"thinking": {"type": "disabled"}, "tool_choice": {"type": "function", "function": {"name": "knowledge_search"}}},
    )
    assert result.tool_calls, "供应商未返回工具调用，兼容降级不能算通过"
    call = result.tool_calls[0]
    assert call["name"] == "knowledge_search"
    arguments = json.loads(call["arguments"])
    assert isinstance(arguments.get("query"), str) and arguments["query"].strip()


@pytest.fixture
def live_business_data(live_embed, monkeypatch, tmp_path):
    """真实上传/索引合成知识，供正式 Agent、工具和报告共用。"""
    from models import Customer
    from seed_factors import GOOD_FACTORS
    from services.rag import knowledge_base, reranker

    monkeypatch.setattr(knowledge_base, "KNOWLEDGE_DATA_DIR", str(tmp_path / "knowledge"))
    monkeypatch.setattr(config, "KNOWLEDGE_VECTOR_STORE", "memory")
    monkeypatch.setattr(vector_store, "_MEMORY_STORE", InMemoryVectorStore())
    monkeypatch.setattr(reranker, "get_reranker", lambda: MetadataReranker())
    # retriever 已导入工厂；固定本地重排算法，不引入额外模型下载。
    monkeypatch.setattr("services.rag.retriever.get_reranker", lambda: MetadataReranker())
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            customer = Customer(customer_name="在线测试合成客户", industry="制造",
                                custom_fields=dict(GOOD_FACTORS))
            db.add(customer)
            db.commit()
            kb = knowledge_base.KnowledgeBaseService(db, embed_func=live_embed)
            doc = kb.create_from_upload(
                title="合成回款制度", category="内部规范", filename="policy.txt",
                raw="账款逾期后，财务负责人应在36小时内完成单据核验，在72小时内取得付款排期。".encode(),
                status="canonical",
            )
            assert doc.index_status == "indexed"
            yield db, customer, doc
    finally:
        engine.dispose()


@pytest.mark.parametrize("draft,expected", [
    ("根据回款制度，财务负责人应在72小时内完成单据核验。", "issues"),
    ("根据回款制度，财务负责人应在36小时内完成单据核验，在72小时内取得付款排期。", "passed"),
    ("建议15天内完成客户访谈，此项为新增建议，不是回款制度规定。", "passed"),
], ids=["wrong-action-deadline", "correct-facts", "proposal-not-fact"])
def test_live_fact_check(live_chat, draft, expected, limits):
    from services.ai.fact_check import messages_for_check, parse_result

    sources = {"rag:1:0": "账款逾期后，财务负责人应在36小时内完成单据核验，在72小时内取得付款排期。"}
    text = "".join(live_chat.stream_chat_completion(
        messages_for_check(draft, sources), temperature=0,
        max_tokens=config.AI_FACT_CHECK_MAX_TOKENS,
        extra={"thinking": {"type": "disabled"}},
        deadline=time.monotonic() + limits[1],
    ))
    result = parse_result(text, draft, sources)
    assert result.status == expected, f"核对状态错误：{result.status}；{result.reason}"
    if expected == "issues":
        assert any(i["source_id"] == "rag:1:0" and "72" in i["claim"]
                   and "36" in i["evidence"] for i in result.issues)


@pytest.mark.parametrize('draft,has_issue', [
    ('OR与CI是主要短板，SVC得分率为60%。', False),
    ('OR-01是OR维度的主要失分点。', False),
    ('OR绝对缺口为所有维度最大。', True),
    ('OR得分率为所有维度最低。', True),
    ('OR-01是OR维度全部失分的来源。', True),
    ('KCR-02中等级1代表中立。', True),
    ('总分78.9，较上次+18.6分。', False),
    ('总分78.9，较上次+30分。', True),
])
def test_live_fact_check_preserves_major_vs_extreme_boundary(live_chat, limits, draft, has_issue):
    from services.ai.fact_check import messages_for_check, parse_result
    from test_fact_check import _score_case

    customer, facts, sources = _score_case()
    sources['levels'] = '关键人客情等级：-1反对、0中立、1支持、2强支持、3教练级。'
    sources['history'] = '本次评估总分78.9，上次评估总分60.3，本次较上次增加18.6分。'
    text = ''.join(live_chat.stream_chat_completion(
        messages_for_check(draft, sources), temperature=0,
        max_tokens=config.AI_FACT_CHECK_MAX_TOKENS,
        extra={'thinking': {'type': 'disabled'}}, deadline=time.monotonic() + limits[1],
    ))
    payload = json.loads(text.removeprefix('```json').removesuffix('```').strip())
    # Check the model's classification too: engine checks must not hide a missed error.
    assert bool(payload['issues']) == has_issue, text
    result = parse_result(text, draft, sources, customer=customer, scoring_facts=facts)
    assert result.status == ('issues' if has_issue else 'passed'), result


def test_live_fact_check_extracts_all_combined_score_targets(live_chat, limits):
    from services.ai.fact_check import messages_for_check, parse_result
    from test_fact_check import _score_case, _with_targets

    customer, facts, sources = _score_case()
    draft = _with_targets('KCR由24→29（+5.0），总分由78.9提升至83.9。')
    text = ''.join(live_chat.stream_chat_completion(
        messages_for_check(draft, sources), temperature=0,
        max_tokens=config.AI_FACT_CHECK_MAX_TOKENS,
        extra={'thinking': {'type': 'disabled'}}, deadline=time.monotonic() + limits[1],
    ))
    result = parse_result(text, draft, sources, customer=customer, scoring_facts=facts)
    assert result.status == 'passed' and not result.reason, (result, text)
    payload = json.loads(text.removeprefix('```json').removesuffix('```').strip())
    assert payload['score_projections']
    for projection in payload['score_projections']:
        assert set(projection['changes']) == {'kcr_01', 'kcr_04', 'kcr_08', 'kcr_10b'}


def test_live_local_patch_preserves_json_strategy_fields(live_chat, limits):
    from services.ai import local_quality
    from test_fact_check import _score_case

    _, _, sources = _score_case()
    item = {'priority': 'recommended', 'urgency': 'high', 'title': '补齐组织绑定',
            'reason': 'OR得分率70%，为所有维度最低。',
            'action': '建议客户经理30天内沟通协议升级，OR-01提升至区域级。',
            'expected_outcome': '记录客户反馈，不预估具体增分。', 'reference': '',
            'score_targets': {'or_01': '区域级'}}
    draft = '```json\n' + json.dumps({'strategies': [item]}, ensure_ascii=False) + '\n```'
    answer = local_quality.parse_answer(draft)
    issues = [{'claim': item['reason'], 'source_id': 'scoring', 'evidence': sources['scoring'],
               'reason': 'OR不是得分率最低维度，最低为SVC的60%。'}]
    text = ''.join(live_chat.stream_chat_completion(
        local_quality.messages_for_patch(answer, {'u0'}, issues, sources), temperature=0,
        max_tokens=config.AI_FACT_CHECK_MAX_TOKENS,
        extra={'thinking': {'type': 'disabled'}}, deadline=time.monotonic() + limits[1],
    ))
    rendered, diagnostic = local_quality.apply_patches(text, answer, {'u0'}, sources)
    revised = local_quality.parse_answer(rendered).units[0].item
    assert diagnostic['patched'] == ['u0']
    assert revised['reason'] != item['reason'] and '为所有维度最低' not in revised['reason']
    assert {key: value for key, value in revised.items() if key != 'reason'} == {
        key: value for key, value in item.items() if key != 'reason'}


@pytest.mark.parametrize("scenario", ["assessment", "strategy"])
def test_live_business_agent(live_chat, live_business_data, scenario, limits, record_property):
    from services.ai.context_builder import build_context
    from services.ai.graph_builder import AssessmentStrategyAgent
    from services.ai.strategy import split_strategy_payload
    from services.scoring import get_scoring_strategy

    db, customer, doc = live_business_data
    # 与 chat_engine 一致：先构建评分展示文本，RAG 留给 Agent 执行。
    ctx = build_context(db, customer, retrieve_knowledge=False)
    started = time.monotonic()
    result = AssessmentStrategyAgent(
        adapter=live_chat, tools_enabled=True,
        deadline=started + limits[1],
    ).run(scenario, ctx, customer, db,
          question="请评估该客户，说明当前评分和风险等级，并结合回款制度给出维护建议，注明参考来源。")
    record(record_property, scenario=scenario, agent_seconds=time.monotonic() - started,
           llm_calls=len(result.timings["llm_calls"]), refine_rounds=result.timings["refine_rounds"])
    assert not result.degraded and result.text.strip()
    assert all(warning == "已修正或移除部分依据不足的结论，保留已核实的分析。"
               for warning in result.warnings), result.warnings
    assert result.timings["verification"][-1]["status"] == "passed"
    assert not result.timings["verification"][-1]["issues"]
    assert not result.timings["verification"][-1]["reason"]
    assert any(r["document_id"] == doc.id for r in result.references)
    expected = get_scoring_strategy().evaluate(customer)
    assert ctx.assessment.total_score == expected.total_score
    assert expected.level in result.text
    assert str(expected.total_score).removesuffix(".0") in result.text
    assert "合成回款制度" in result.text
    if scenario == "strategy":
        _, items = split_strategy_payload(result.text)
        assert items and all(i["title"] and i["action"] for i in items)


def test_live_stream_tool_roundtrip(live_chat, live_business_data, limits, record_property):
    from services.ai import tools

    db, customer, doc = live_business_data
    messages = [LLMMessage("user", "请调用knowledge_search查询回款制度，再回答单据核验和付款排期各自的小时数，并注明参考来源。")]
    calls = []
    deadline = time.monotonic() + limits[1]
    list(live_chat.stream_chat_completion(
        messages, tools=tools.TOOL_SCHEMAS, max_tokens=512,
        extra={"thinking": {"type": "disabled"}},
        on_tool_calls=calls.extend, deadline=deadline,
    ))
    assert calls and any(c["name"] == "knowledge_search" for c in calls)
    messages, references = tools.append_tool_results(messages, calls, customer=customer, db=db)
    assert any(r["document_id"] == doc.id for r in references)
    tool_messages = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
    assert tool_messages and all(m["tool_call_id"] in {c["id"] for c in calls} for m in tool_messages)
    messages.append(LLMMessage("user", '请仅依据工具返回的制度输出纯JSON：'
                               '{"verification_hours":单据核验小时数,"payment_schedule_hours":取得付款排期小时数,"reference":知识标题}。'))
    answer = "".join(live_chat.stream_chat_completion(
        messages, max_tokens=512, extra={"thinking": {"type": "disabled"}}, deadline=deadline,
    ))
    record(record_property, tool_calls=len(calls))
    facts = json.loads(answer)
    assert facts["verification_hours"] == 36
    assert facts["payment_schedule_hours"] == 72
    assert facts["reference"] == "合成回款制度"


def test_live_report_ai(live_chat, live_business_data, record_property, tmp_path, monkeypatch):
    from services.report_builder import build_report_data
    from seed_factors import RISKY_FACTORS
    from scripts.quality_evidence import QualityEvidence

    db, customer, doc = live_business_data
    # 引用验收需要制度实际适用：原夹具为满分、零合同的新客户，
    # 生成一般经营建议而不引用逾期制度符合生产契约。
    customer.custom_fields = dict(RISKY_FACTORS)
    customer.contract_amount = 100
    customer.cooperation_years = 3
    customer.notes = "当前已有一笔合同应收款逾期，尚未完成单据核验与付款排期确认。"
    db.commit()
    started = time.monotonic()
    recorder = QualityEvidence(tmp_path / 'quality-trace', secrets=[config.LLM_API_KEY, config.EMBEDDING_API_KEY])
    monkeypatch.setattr(llm_adapter, '_chat_adapter', recorder.adapter(live_chat))
    with recorder.parsers():
        report = build_report_data(db, customer, include_ai=True)
    evidence = tmp_path / "report-ai-evidence.json"
    evidence.write_text(json.dumps({
        "customer_notes": customer.notes,
        "strategy_items": report.strategy_items, "references": report.references,
        "warnings": report.warnings, "error": report.error, "timings": report.timings,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    record(record_property, report_seconds=time.monotonic() - started,
           report_llm_calls=len(report.timings.get("llm_calls", [])), report_artifact=str(evidence))
    assert report.has_ai and not report.degraded, report.error
    assert report.error is None, report.error
    # 成功局部修复是合法恢复；仍拒绝降级、未核实和其他异常提示。
    assert all(warning == "已修正或移除部分依据不足的结论，保留已核实的分析。"
               for warning in report.warnings), report.warnings
    assert report.strategy_items and all(i["title"] and i["action"] for i in report.strategy_items)
    assert any(r["document_id"] == doc.id for r in report.references)
    assert any("合成回款制度" in i.get("reference", "") for i in report.strategy_items), f"策略引用缺失，详见 {evidence}"
    assert report.timings["verification"][-1]["status"] == "passed"
    assert not report.timings["verification"][-1]["issues"]
    assert not report.timings["verification"][-1]["reason"]
    assert report.timings["llm_calls"][0]["max_tokens"] == config.LLM_REPORT_MAX_TOKENS


@pytest.mark.parametrize('draft,has_issue', [
    ('账款逾期后，财务负责人36小时内完成单据核验、72小时内取得付款排期。', False),
    ('按制度，财务负责人必须在72小时内取得客户书面付款排期。', True),
    ('按制度72小时内取得付款排期；建议另行取得客户书面确认。', False),
    ('该制度仅要求72小时内取得付款排期，无需单据核验。', True),
    ('账款逾期后36小时核验、72小时取得排期，本策略15个工作日内完成上述核验和排期。', True),
    ('账款逾期后36小时核验、72小时取得排期；客户经理另在15个工作日内完成风险复盘。', False),
])
def test_live_policy_requirement_and_recommendation_pairs(live_chat, limits, draft, has_issue):
    from services.ai.fact_check import messages_for_check, parse_result

    sources = {'knowledge': '账款逾期后，财务负责人应在36小时内完成单据核验，在72小时内取得付款排期。'}
    raw = ''.join(live_chat.stream_chat_completion(
        messages_for_check(draft, sources), temperature=0, max_tokens=config.AI_FACT_CHECK_MAX_TOKENS,
        extra={'thinking': {'type': 'disabled'}}, deadline=time.monotonic() + limits[1],
    ))
    payload = json.loads(raw.removeprefix('```json').removesuffix('```').strip())
    assert bool(payload['issues']) == has_issue, raw
    checked = parse_result(raw, draft, sources)
    assert checked.status == ('issues' if has_issue else 'passed'), (checked, raw)
