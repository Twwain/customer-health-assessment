"""Proof-bound catalog. Text is registered server output, never model output."""
from __future__ import annotations

import json

from services.ai.guardrails import sanitize_output


def quoted(value):
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    # Keep user data out of Markdown syntax and HTML executable contexts.
    text = sanitize_output(text).replace("\n", " ")
    # React's lightweight Markdown and ReportLab have different entity rules.
    # Fullwidth display escaping prevents both markup injection and visible
    # double-escaped entities, while the private fact retains the exact raw value.
    return text.translate(str.maketrans("<>&`*_[]()#!|", "＜＞＆｀＊＿［］（）＃！｜"))


def build_catalog(package):
    nodes = {}
    facts = package["facts"]

    def node(ref, template, refs, sections, predicate, label, coverage=()):
        sources = sorted({s for f in refs for s in facts[f]["source_refs"]})
        nodes[ref] = dict(id=ref, template_id=template, slots={"fact_refs": refs}, fact_refs=refs,
            source_refs=sources, sections=sections, predicate_id=predicate, label=label,
            coverage_keys=list(coverage))

    score = facts["f:score"]["value"]
    node("r:score", "template:score_grade", ["f:score"], ["summary", "explanation"], "engine_result",
         f"按冻结记录和评分配置，引擎实际总分 {score['total']:g}/{score['max']:g}，等级为{quoted(score['grade'])}。",
         ["score", "grade"])
    quality = facts["f:quality"]["value"]
    node("r:quality", "template:quality_counts", ["f:quality"], ["data_quality", "explanation"], "engine_result",
        f"当前启用因子 {quality['enabled_factor_count']} 项；其中缺失 {quality['missing_factor_count']} 项、非缺失但格式需核实 {quality['invalid_factor_count']} 项；实际预警 {quality['alert_count']} 条。三类数量口径分别统计。")
    from .protocol import fingerprint
    evidence_status = package["retrieval_status"]
    package["sources"]["src:retrieval"] = dict(id="src:retrieval", kind="knowledge", record_ref="retrieval:"+package["snapshot_id"].split(":")[1],
        content_hash=fingerprint([evidence_status, len(package["evidence"])]), visibility_state="active", source_class="general_knowledge")
    facts["f:evidence_status"] = dict(id="f:evidence_status", subject_ref=package["customer_ref"], kind="count",
        value=dict(status=evidence_status, hits=len(package["evidence"])), state="known", input_valid="not_applicable",
        source_refs=["src:retrieval"], derivation=dict(operator="raw", operator_version="2.1", fact_refs=[]))
    evidence_text = ("本次检索未找到可引用条款，不能确认制度义务或适用性；建议补充具体制度文件及适用范围后核实。"
                     if evidence_status == "no_hits" else "本次资料未完成适用性确认；通用参考不能当作客户现状或已经发生的义务。")
    node("r:evidence_status", "template:evidence_status", ["f:evidence_status"], ["policy"], "retrieval_state", evidence_text)
    for factor in package["factors"]:
        field, label = factor["field"], quoted(factor["label"])
        fid, rid = "f:"+field, "r:"+field
        missing = factor["state"] == "missing"
        text = f"记录未提供{label}，当前不能确认该项现状。" if missing else f"记录显示{label}原值为「{quoted(factor['value'])}」。"
        text += f"引擎该因子实际计分 {factor['score']:g} 分。"
        if factor["input_valid"] == "invalid":
            text += "原值未符合当前表单格式，需核实；不能据此改写引擎实际计分或认定为合法最低档。"
        node(rid, "template:field_score", [fid], ["reason", "explanation", "data_quality"],
             "missing_record" if missing else "has_record", text)
    for fid, fact in list(facts.items()):
        if fact["kind"] == "count" and fact["derivation"]["operator"] == "count_complete_list":
            c = fact["value"]
            node("r:"+fid[2:], "template:people_counts", [fid], ["explanation", "data_quality"], "count_from_complete_list",
                f"完整逐人样本共 {c['total']} 人：反对 {c['opposed']} 人、中立 {c['neutral']} 人、至少支持 {c['support_or_above']} 人、恰为强支持 {c['exact_strong']} 人、至少强支持 {c['at_least_strong']} 人、教练级 {c['coach']} 人。"
                "以上仅描述该完整样本，不能代替支持度字段的统计口径或Champion记录；口径差异需并列核实。")
    factor_refs = ["f:"+f["field"] for f in package["factors"]]
    node("r:factor_overview", "template:all_factor_records", factor_refs, ["explanation", "data_quality"], "record_state_union",
         "\n".join(nodes["r:"+f["field"]]["label"] for f in package["factors"]))
    if package.get("comparison_members"):
        comparison_refs = ["f:score"]
        labels = ["当前客户："+nodes["r:score"]["label"]]
        for member in package["comparison_members"]:
            prefix = "c"+str(member["raw"]["id"])+"_"
            for source_ref, source in member["sources"].items():
                copied = dict(source, id="src:"+prefix+source_ref.split(":", 1)[1])
                package["sources"][copied["id"]] = copied
            score_fact = dict(member["facts"]["f:score"])
            score_fact["id"] = "f:"+prefix+"score"
            score_fact["source_refs"] = ["src:"+prefix+s.split(":", 1)[1] for s in score_fact["source_refs"]]
            facts[score_fact["id"]] = score_fact
            comparison_refs.append(score_fact["id"])
            value = score_fact["value"]
            labels.append(f"客户 {member['raw']['id']}「{quoted(member['raw']['customer_name'])}」：引擎实际总分 {value['total']:g}/{value['max']:g}，等级{quoted(value['grade'])}。")
        node("r:comparison", "template:compare_scores", comparison_refs, ["comparison"], "engine_result",
             "\n".join(labels)+"\n以上比较仅针对各自冻结记录和同次评分配置，不代表差异的业务原因。")
    for evidence in package["evidence"]:
        fid = "f:knowledge"+str(evidence["chunk_id"])
        facts[fid] = dict(id=fid, subject_ref=package["customer_ref"], kind="attributed_quote", value=evidence["content"],
            state="known", input_valid="not_applicable", source_refs=["src:"+str(evidence["chunk_id"])],
            derivation=dict(operator="raw", operator_version="2.1", fact_refs=[]))
        node("r:knowledge"+str(evidence["chunk_id"]), "template:knowledge_quote", [fid], ["policy"], "quoted_source",
             f"资料「{quoted(evidence['title'])}」原文引用：{quoted(evidence['content'])}。\n"
             "该资料为通用参考，尚未确认本客户适用性；不能据此认定客户已有对应资产、违反制度或起算义务。")
    alerts = sorted(package["assessment"]["alerts"], key=lambda a: ({"high": 0, "medium": 1, "low": 2}.get(a["level"], 3), a["id"]))
    for i, alert in enumerate(alerts):
        # Pre-existing engine messages are attributed to the configured engine,
        # never promoted to independent causal claims.
        fid = f"f:alert{i}"
        facts[fid] = dict(id=fid, subject_ref=package["customer_ref"], kind="engine_score", value=alert,
            state="known", input_valid="not_applicable", source_refs=["src:customer", "src:config", "src:history"],
            derivation=dict(operator="engine", operator_version="2.1", fact_refs=["f:score"]))
        node(f"r:alert{i}", "template:engine_alert", [fid], ["risk"], "engine_result",
             "评分引擎预警："+quoted(alert["message"]), ["alert:"+alert["id"]])
    history = package["history"][-6:]
    fid = "f:trend"
    facts[fid] = dict(id=fid, subject_ref=package["customer_ref"], kind="trend",
        value=dict(points=history, window=[history[0]["assessed_at"], history[-1]["assessed_at"]] if history else []),
        state="known" if len(history) >= 2 else "missing", input_valid="not_applicable",
        source_refs=["src:history"], derivation=dict(operator="trend_window", operator_version="2.1", fact_refs=[]))
    text = "历史点不足，不能判断连续趋势。"
    if len(history) >= 2:
        values = [r["total_score"] for r in history]
        changes = [round(b-a, 6) for a, b in zip(values, values[1:])]
        text = (f"历史窗口 {history[0]['assessed_at']} 至 {history[-1]['assessed_at']}（UTC），"
                f"{len(values)} 个历史点、{len(changes)} 次变化，分数为 "+" → ".join(f"{v:g}" for v in values)+"。"
                "最新两点"+("持平" if abs(changes[-1]) < .05 else f"变化 {changes[-1]:+g} 分")+
                "；这些历史记录不证明具体业务原因或原业务因素已经停止，配置不同时不可直接归因为客情变化。")
    node("r:trend", "template:trend_window", [fid], ["trend"], "trend_all_steps", text)
    options = {}
    for factor in package["factors"]:
        if factor["state"] != "known" or factor["input"]["type"] == "key_person_levels":
            continue
        for i, value in enumerate(factor["input"]["options"]):
            ref = f"option:{factor['field']}.{i}"
            options[ref] = dict(id=ref, field_ref="field:"+factor["field"], value=value)
    return dict(render_nodes=nodes, projection_options=options)
