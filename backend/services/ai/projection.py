"""What-if computations use the frozen clock and the real scoring evaluator."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from services.ai.scoring_facts import _legal_target, _summary
from services.scoring.config_loader import load_scoring_config
from .protocol import fingerprint, ProtocolError


def project(package, changes):
    config = load_scoring_config()
    if fingerprint(asdict(config)) != package["config_hash"]:
        raise ProtocolError("source_changed")
    factors = {f.field: f for dim in config.enabled_dimensions for f in dim.enabled_factors}
    original = SimpleNamespace(**deepcopy(package["raw"]))
    today = datetime.fromisoformat(package["captured_at"]).astimezone(timezone(timedelta(hours=8))).date()
    before = _summary(original, config, today)
    target = deepcopy(original)
    for field, value in changes.items():
        factor = factors.get(field)
        fact = package["facts"].get("f:"+field)
        if not factor or not fact or fact["state"] != "known":
            raise ProtocolError("unknown_projection_baseline")
        normalized = _legal_target(factor, value)
        if factor.source == "custom_fields":
            target.custom_fields[field] = normalized
        else:
            setattr(target, field, normalized)
    after = _summary(target, config, today)
    return dict(before=before, after=after, delta=round(after["raw_total_score"]-before["raw_total_score"], 6))
