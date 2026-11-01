"""Capture authoritative inputs in a short read transaction; never infer prose facts."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from uuid import uuid4
from contextlib import nullcontext

from sqlalchemy.orm import Session

import config
from models import Customer, AssessmentHistory
from services.scoring.config_driven import ConfigDrivenStrategy
from services.scoring.config_loader import load_scoring_config, KEY_PERSON_COUNT
from services.scoring.rules import resolve_value, evaluate_factor
from services.ai.scoring_facts import _legal_target, _json_value
from services import assessment_history
from .protocol import fingerprint


def customer_values(customer):
    return {c.name: _json_value(deepcopy(getattr(customer, c.name))) for c in Customer.__table__.columns}


def history_values(rows):
    # AI strategy attachments do not change score/trend source validity.
    return [{k: _json_value(deepcopy(getattr(row, k))) for k in
             ("id", "customer_id", "total_score", "max_score", "level", "dimensions",
              "risk_alerts", "factor_snapshot", "config_version", "assessed_at")} for row in rows]


def capture(bind, customer_id, *, clock=None, reader=None):
    clock = clock or datetime.now(timezone.utc)
    today = clock.astimezone(timezone(timedelta(hours=8))).date()
    scoring = ConfigDrivenStrategy()
    scoring_config = load_scoring_config()
    with (nullcontext(reader) if reader is not None else Session(bind=bind)) as db:
        # Explicit SQLite BEGIN gives all SELECTs a single WAL read snapshot.
        if reader is None and bind.dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN")
        customer = db.get(Customer, customer_id)
        if customer is None:
            raise LookupError("customer_missing")
        raw = customer_values(customer)
        rows = (db.query(AssessmentHistory).filter(AssessmentHistory.customer_id == customer_id,
                AssessmentHistory.assessed_at <= clock.replace(tzinfo=None))
                .order_by(AssessmentHistory.assessed_at, AssessmentHistory.id).all())
        historical = history_values(rows)
        assessment = scoring.evaluate(customer, today=today)
        assessment_history.apply_trend_alerts(db, customer, assessment, as_of=clock.replace(tzinfo=None))
        trend = assessment_history.build_trend(db, customer, limit=config.CHAT_TREND_POINTS,
                                                fallback_assessment=assessment)
        factor_values = []
        for dim in scoring_config.enabled_dimensions:
            for factor in dim.enabled_factors:
                value = _json_value(deepcopy(resolve_value(customer, factor)))
                missing = value is None or isinstance(value, str) and not value.strip()
                try:
                    _legal_target(factor, value)
                    valid = "valid"
                except ValueError:
                    valid = "invalid" if not missing else "not_applicable"
                factor_values.append(dict(field=factor.field, label=factor.label, dimension=dim.key,
                    value=value, state="missing" if missing else "known", input_valid=valid,
                    score=evaluate_factor(factor, value, today).score, input=asdict(factor.input),
                    rule=asdict(factor.rule), source=factor.source))
    config_value = asdict(scoring_config)
    # Private immutable-by-value package. Outbound views are built separately.
    content = dict(customer=raw, config=config_value, history=historical, scoring_date=today.isoformat())
    package = dict(snapshot_id="snapshot:"+uuid4().hex, captured_at=clock.isoformat(),
        customer_ref=f"customer:{customer_id}", source_fingerprint=fingerprint(content),
        config_hash=fingerprint(config_value), customer_hash=fingerprint(raw), history_hash=fingerprint(historical),
        catalog_version="2.1", raw=raw, history=historical, config=config_value, factors=factor_values,
        assessment=assessment.model_dump(mode="json"), trend=trend.model_dump(mode="json"),
        evidence=[], retrieval_status="not_requested", facts={})
    subject = package["customer_ref"]
    package["snapshot"] = dict(id=package["snapshot_id"], captured_at=package["captured_at"],
        members={subject: package["customer_hash"]}, config_hash=package["config_hash"], history_hash=package["history_hash"],
        evidence_hashes=[], catalog_version="2.1")
    package["sources"] = {"src:"+kind: dict(id="src:"+kind, kind=kind,
        record_ref=subject if kind == "customer" else kind+":active", content_hash=hash_value,
        visibility_state="active", source_class=source_class) for kind, hash_value, source_class in (
            ("customer", package["customer_hash"], "customer_record"), ("config", package["config_hash"], "scoring"),
            ("history", package["history_hash"], "history"))}
    for f in factor_values:
        kind = "enum" if f["input"]["options"] else "raw_scalar"
        value = f["value"]
        if f["input"]["type"] == "key_person_levels":
            import json
            try:
                people = json.loads(value) if isinstance(value, str) else value
            except ValueError:
                people = None
            complete = f["input_valid"] == "valid" and isinstance(people, list) and len(people) == KEY_PERSON_COUNT
            kind, value = "person_list", dict(records=people if complete else [], complete=complete)
        fid = "f:"+f["field"]
        package["facts"][fid] = dict(id=fid, subject_ref=subject, kind=kind, value=value,
            state=f["state"], input_valid=f["input_valid"], source_refs=["src:customer"],
            derivation=dict(operator="raw", operator_version="2.1", fact_refs=[]))
    package["facts"]["f:score"] = dict(id="f:score", subject_ref=subject, kind="engine_score",
        value=dict(total=assessment.total_score, max=assessment.max_score, grade=assessment.level),
        state="known", input_valid="not_applicable", source_refs=["src:customer", "src:config"],
        derivation=dict(operator="engine", operator_version=scoring_config.version, fact_refs=[]))
    # Counts have distinct subjects. Never treat missing records as an enum
    # value, nor reverse an average interval into individual people.
    quality = dict(enabled_factor_count=len(factor_values), alert_count=len(assessment.alerts),
        missing_factor_count=sum(f["state"] == "missing" for f in factor_values),
        invalid_factor_count=sum(f["input_valid"] == "invalid" for f in factor_values))
    package["facts"]["f:quality"] = dict(id="f:quality", subject_ref=subject, kind="count", value=quality,
        state="known", input_valid="not_applicable", source_refs=["src:customer", "src:config"],
        derivation=dict(operator="engine", operator_version="2.1", fact_refs=["f:"+f["field"] for f in factor_values]))
    for fact in list(package["facts"].values()):
        if fact["kind"] == "person_list" and fact["value"]["complete"]:
            records = fact["value"]["records"]
            counts = {"total": len(records), "opposed": sum(int(v) == -1 for v in records),
                "neutral": sum(int(v) == 0 for v in records), "support_or_above": sum(int(v) >= 1 for v in records),
                "exact_strong": sum(int(v) == 2 for v in records), "at_least_strong": sum(int(v) >= 2 for v in records),
                "coach": sum(int(v) == 3 for v in records)}
            fid = fact["id"]+"_counts"
            package["facts"][fid] = dict(id=fid, subject_ref=subject, kind="count", value=counts, state="known",
                input_valid="not_applicable", source_refs=["src:customer"],
                derivation=dict(operator="count_complete_list", operator_version="2.1", fact_refs=[fact["id"]]))
    return package


def current_fingerprint(bind, package):
    if package.get("comparison_members"):
        return capture_comparison(bind, package["raw"]["id"], [p["raw"]["id"] for p in package["comparison_members"]])["source_fingerprint"]
    return capture(bind, package["raw"]["id"])["source_fingerprint"]


def explicit_comparison_ids(db, question, customer_id):
    """Only explicitly named records can enter the model's subject allowlist."""
    import re
    if "对比" not in question and "比较" not in question:
        return []
    names = db.query(Customer.id, Customer.customer_name).all()
    selected = {cid for cid, name in names if name and name in question}
    selected.update(int(v) for v in re.findall(r"(?:customer:|客户\s*)(\d+)(?!\d)", question))
    permitted = {cid for cid, _ in names}
    if not selected.issubset(permitted) or len(selected | {customer_id}) > 8:
        from .protocol import ProtocolError
        raise ProtocolError("needs_clarification")
    return sorted(selected-{customer_id})


def capture_comparison(bind, customer_id, others):
    clock = datetime.now(timezone.utc)
    with Session(bind=bind) as reader:
        if bind.dialect.name == "sqlite":
            reader.connection().exec_driver_sql("BEGIN")
        package = capture(bind, customer_id, clock=clock, reader=reader)
        members = [capture(bind, cid, clock=clock, reader=reader) for cid in sorted(set(others)-{customer_id})]
    package["comparison_members"] = members
    package["source_fingerprint"] = fingerprint([package["source_fingerprint"], [p["source_fingerprint"] for p in members]])
    for member in members:
        package["snapshot"]["members"].update(member["snapshot"]["members"])
    return package
