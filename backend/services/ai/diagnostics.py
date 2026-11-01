"""Private bounded protocol diagnostics; never retain arbitrary model text."""
import hashlib
import json
import re

from .protocol import fingerprint

SECTIONS = {"summary", "risk", "strength", "reason", "trend", "policy", "explanation", "comparison", "data_quality"}
DETAIL_SECTIONS = {"trend", "policy", "explanation", "comparison", "data_quality"}
ROOT_KEYS = {"intents", "status", "version", "clarification_code", "summary_refs", "risks", "detail_refs",
             "actions", "verification_questions", "projections", "strength_refs", "hypotheses"}
ARGUMENT_KEYS = {"kind", "subject_refs", "field_refs", "option_refs", "source_refs", "count", "section",
                "render_ref", "changes", "id", "purpose", "priority", "slot_id", "value"}
KNOWN_KEYS = ROOT_KEYS | ARGUMENT_KEYS | {"schema_version","snapshot_id","request_contract_id",
    "base_plan_hash","replacements"}
_MISSING = object()


def _safe_pointer(pointer):
    parts = pointer.strip("/").split("/") if pointer else []
    if not parts or parts[0] not in ROOT_KEYS:
        return ""
    result = [parts[0]]
    if len(parts)>1 and parts[1].isdigit() and int(parts[1])<1000:
        result.append(parts[1])
    if len(parts)>len(result) and parts[len(result)] in ARGUMENT_KEYS:
        result.append(parts[len(result)])
    return "/" + "/".join(result)


def _at(value, pointer):
    try:
        for part in pointer.strip("/").split("/") if pointer else []:
            value = value[int(part)] if isinstance(value, list) else value[part]
        return value
    except (KeyError, IndexError, TypeError, ValueError):
        return _MISSING


def _known_ids(package):
    known = set(package.get("sources", {})) | set(package.get("facts", {}))
    known.update('r:'+f[2:] for f in package.get('facts',{}) if f.startswith('f:'))
    known.update(package.get("snapshot", {}).get("members", {}))
    for factor in package.get("factors", []):
        field=factor["field"]
        known.update(("field:"+field, "r:"+field))
        known.update(f"option:{field}.{i}" for i,_ in enumerate(factor["input"].get("options",[])))
    known.update(("r:score","r:quality","r:evidence_status","r:factor_overview","r:trend","r:comparison"))
    known.update("r:alert"+str(i) for i,_ in enumerate(package.get("assessment",{}).get("alerts",[])))
    known.update("r:knowledge"+str(e["chunk_id"]) for e in package.get("evidence",[]))
    return known


def parameter_shapes(value, errors, package, *, stage="before_intent", rule_version="1.1"):
    """Hash unknown values, expose only registered paths and known server IDs."""
    stage=stage if stage in ("before_intent","before_plan","before_repair","failure") else "failure"
    known=_known_ids(package)
    shapes=[]
    for error in errors[:24]:
        pointer=_safe_pointer(error.pointer)
        item=_at(value,pointer)
        invalid_json=error.code in ("invalid_json","duplicate_key","non_finite_number")
        raw=value if invalid_json and isinstance(value,str) else None
        kind=("missing" if item is _MISSING else "null" if item is None else "boolean" if type(item) is bool
              else "integer" if type(item) is int else "number" if type(item) is float else "string" if isinstance(item,str)
              else "array" if isinstance(item,list) else "object")
        digest=None
        try:
            if raw is not None:
                digest=hashlib.sha256(raw.encode()).hexdigest()
            elif item is not _MISSING:
                digest=fingerprint(dict(json_type=kind,value=item))
        except (ValueError,TypeError,RecursionError,OverflowError):
            pass
        allowed=[r for r in error.allowed_refs if isinstance(r,str) and r in known]
        allowed=list(dict.fromkeys(allowed))
        shape=dict(stage=stage,error_code=error.code if re.fullmatch(r'[a-z_]{1,50}',error.code) else "invalid_protocol",
            pointer=pointer,json_type="invalid_json" if invalid_json else kind,value_sha256=digest,
            response_bytes=len(raw.encode()) if raw is not None else None,allowed_count=len(error.allowed_refs),
            allowed_ids=allowed[:8],truncated_count=max(0,len(error.allowed_refs)-len(allowed[:8])),
            unknown_key_count=len(set(item)-KNOWN_KEYS) if isinstance(item,dict) else 0,
            rule_version=rule_version,source_fingerprint=package.get("source_fingerprint") or None)
        if len(json.dumps(shapes+[shape],ensure_ascii=False,separators=(",",":")).encode())>8192:
            break
        shapes.append(shape)
    return shapes


def detail_shape(value, package, catalog):
    if not isinstance(value, dict):
        return {"json_type": type(value).__name__}
    ref, section = value.get("render_ref"), value.get("section")
    known = {ref for group in catalog.values() for ref in group} | set(package["facts"]) | set(package["sources"])
    node = catalog["render_nodes"].get(ref) if isinstance(ref, str) else None
    return dict(json_type="object", known_render_ref=ref if isinstance(ref,str) and ref in known else None,
        unknown_ref_hash=fingerprint(ref) if ref is not None and (not isinstance(ref,str) or ref not in known) else None,
        registered_section=section if isinstance(section,str) and section in SECTIONS else None,
        unknown_section_hash=fingerprint(section) if section is not None and (not isinstance(section,str) or section not in SECTIONS) else None,
        legal_detail_sections=sorted(set(node["sections"]) & DETAIL_SECTIONS) if node else [],
        known_keys=sorted(set(value) & {"section", "render_ref", "priority", "id", "slot_id", "value"}),
        unknown_key_count=len(set(value)-{"section", "render_ref", "priority", "id", "slot_id", "value"}))


def plan_shapes(plan, package, catalog):
    details = plan.get("detail_refs") if isinstance(plan, dict) else None
    return [dict(index=i, **detail_shape(value, package, catalog)) for i, value in enumerate(details[:24])] if isinstance(details,list) else []


def repair_shapes(response, envelope, package, catalog):
    replacements = response.get("replacements") if isinstance(response,dict) else None
    if not isinstance(replacements,list):
        return {"has_replacements_array": False}
    return {"has_replacements_array": True, "items": [dict(
        known_slot=r.get("slot_id") if isinstance(r,dict) and isinstance(r.get("slot_id"),str) and r["slot_id"] in envelope.paths else None,
        value_shape=detail_shape(r.get("value"),package,catalog) if isinstance(r,dict) else {"json_type":type(r).__name__})
        for r in replacements[:24]]}
