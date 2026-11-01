"""Strict decoding and the reviewed, immutable protocol assets."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from dataclasses import dataclass

from jsonschema import Draft202012Validator

# The sole runtime asset directory is verified and read into one bundle per request.
ASSETS = Path(__file__).with_name("assets")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class ProtocolError(ValueError):
    def __init__(self, code, pointer="", allowed_refs=(), dependency_pointers=()):
        super().__init__(code)
        self.code, self.pointer = code, pointer
        self.allowed_refs = list(allowed_refs)
        self.dependency_pointers = list(dependency_pointers)

    def as_dict(self):
        return dict(code=self.code, pointer=self.pointer, allowed_refs=self.allowed_refs,
                    dependency_pointers=self.dependency_pointers)


def strict_json(raw, max_chars=120000):
    if not isinstance(raw, str) or len(raw) > max_chars:
        raise ProtocolError("invalid_json")
    raw = raw.strip()
    if raw.startswith("```json\n") and raw.endswith("\n```"):
        raw = raw[8:-4]

    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                raise ProtocolError("duplicate_key")
            out[k] = v
        return out

    def invalid_constant(_):
        raise ProtocolError("non_finite_number")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError("invalid_json") from exc


@dataclass(frozen=True)
class RuntimeAssets:
    prompts: dict
    schema: dict
    manifest: dict
    rules: dict
    intent_schema: dict
    input_schema: dict
    audit_schema: dict
    diagnostic_schema: dict


def load_runtime_assets():
    """One verified by-value bundle per request; no import-time asset IO."""
    names = ('prompts.json', 'plan.schema.json', 'intent.rules.json', 'intent.schema.json',
             'intent.input.schema.json', 'audit.schema.json', 'diagnostic.schema.json')
    try:
        manifest = json.loads((ASSETS/'manifest.json').read_text(encoding='utf-8'))
        raw = {name:(ASSETS/name).read_bytes() for name in names}
        if set(manifest['sha256']) != set(names) or any(
                hashlib.sha256(raw[n]).hexdigest() != manifest['sha256'][n] for n in names):
            raise ValueError('asset mismatch')
        data = [json.loads(raw[n]) for n in names]
        if not isinstance(data[0], dict) or any(
                not isinstance(lines, list) or not all(isinstance(line, str) for line in lines)
                for lines in data[0].values()):
            raise ValueError('invalid prompt lines')
        prompts = {name: '\n'.join(lines) for name, lines in data[0].items()}
        return RuntimeAssets(prompts,data[1],manifest,data[2],data[3],data[4],data[5],data[6])
    except (OSError,ValueError,KeyError,TypeError) as exc:
        raise ProtocolError('asset_integrity_error') from exc


def load_assets():
    assets = load_runtime_assets()
    return assets.prompts, assets.schema, assets.manifest


def schema_errors(plan, schema):
    errors = []
    for e in Draft202012Validator(schema).iter_errors(plan):
        path = "/" + "/".join(str(x).replace("~", "~0").replace("/", "~1") for x in e.absolute_path)
        if e.validator == "required" and isinstance(e.instance, dict):
            for key in e.validator_value:
                if key not in e.instance:
                    errors.append(ProtocolError("plan_schema", path.rstrip("/")+"/"+key))
        elif e.validator == "uniqueItems" and isinstance(e.instance, list):
            seen = set()
            for i, value in enumerate(e.instance):
                digest = fingerprint(value)
                if digest in seen:
                    errors.append(ProtocolError("duplicate_reference", path.rstrip("/")+f"/{i}"))
                seen.add(digest)
        else:
            errors.append(ProtocolError("plan_schema", path.rstrip("/")))
    return errors


def pointer_get(tree, parts):
    for p in parts:
        tree = tree[int(p)] if isinstance(tree, list) else tree[p]
    return tree


class RepairEnvelope:
    """Private immutable position map; model never controls JSON pointers."""
    @classmethod
    def from_specs(cls,plan,schema,specs):
        """Explicit joint allocation, independent of diagnostic error count."""
        value=cls.__new__(cls)
        value.base=deepcopy(plan);value.base_hash=fingerprint(plan);value.schema=deepcopy(schema)
        value.slots=[];value.paths={}
        paths=[tuple(s['path']) for s in specs]
        if len(set(paths))!=len(paths) or any(p!=q and q[:len(p)]==p for p in paths for q in paths):
            raise ProtocolError('invalid_repair_slot')
        for spec,path in zip(specs,paths):
            operation='replace'
            try:pointer_get(plan,path)
            except (KeyError,IndexError,TypeError):operation='append'
            slot_id=f'slot:s{len(value.slots)+1}'
            value.paths[slot_id]=path
            value.slots.append(dict(slot_id=slot_id,operation=operation,schema=deepcopy(spec['schema']),
                allowed_refs=list(spec.get('allowed_refs',[])),assigned_local_id=spec.get('assigned_local_id')))
        value.frozen=value._frozen_leaves(plan)
        return value

    def _frozen_leaves(self, value, path=()):
        if any(path[:len(p)] == p for p in self.paths.values()):
            return {}
        if isinstance(value, (dict, list)):
            entries = value.items() if isinstance(value, dict) else enumerate(value)
            out = {}
            for k, v in entries:
                out.update(self._frozen_leaves(v, path+(str(k),)))
            return out
        return {path: fingerprint(value)}

    def public(self, errors):
        return dict(base_plan_hash=self.base_hash,
                    frozen_pointer_hashes={"/"+"/".join(p): h for p, h in self.frozen.items()},
                    replace_slots=[s for s in self.slots if s["operation"] == "replace"],
                    append_slots=[s for s in self.slots if s["operation"] == "append"],
                    errors=[e.as_dict() for e in errors])

    def output_schema(self):
        variants = []
        for s in self.slots:
            variants.append(dict(type="object", additionalProperties=False, required=["slot_id", "value"],
                                 properties={"slot_id": {"const": s["slot_id"]}, "value": s["schema"]}))
        return {"type": "object", "additionalProperties": False, "required": ["base_plan_hash", "replacements"],
                "properties": {"base_plan_hash": {"const": self.base_hash}, "replacements": {
                    "type": "array", "minItems": len(self.slots), "maxItems": len(self.slots),
                    "items": {"oneOf": variants}}}, "$defs": self.schema["$defs"]}

    def merge(self, response):
        if not self.slots:
            raise ProtocolError("invalid_repair")
        violations = schema_errors(response, self.output_schema())
        if violations:
            raise ProtocolError("invalid_repair", violations[0].pointer,
                                dependency_pointers=[e.pointer for e in violations[1:]])
        out = deepcopy(self.base)
        seen = set()
        for replacement in response["replacements"]:
            slot = replacement["slot_id"]
            if slot in seen or slot not in self.paths:
                raise ProtocolError("invalid_repair_slot")
            seen.add(slot)
            parts = self.paths[slot]
            if not parts:
                out = replacement["value"]
                continue
            parent = pointer_get(out, parts[:-1])
            key = int(parts[-1]) if isinstance(parent, list) else parts[-1]
            # Append positions are resolved as a batch below, independent of
            # response order and without array insertion shifts.
            if isinstance(parent, dict) or key < len(parent):
                parent[key] = replacement["value"]
        for field in ("summary_refs", "risks", "strength_refs", "actions", "verification_questions", "detail_refs", "projections", "hypotheses", "citation_refs"):
            appends = [(int(self.paths[r["slot_id"]][1]), r["value"]) for r in response["replacements"]
                       if len(self.paths[r["slot_id"]]) == 2 and self.paths[r["slot_id"]][0] == field
                       and isinstance(out, dict) and isinstance(out.get(field), list)
                       and int(self.paths[r["slot_id"]][1]) >= len(out[field])]
            for index, value in sorted(appends):
                if index != len(out[field]):
                    raise ProtocolError("repair_position_gap")
                out[field].append(value)
        for path, expected in self.frozen.items():
            if fingerprint(pointer_get(out, path)) != expected:
                raise ProtocolError("frozen_value_changed")
        return out
