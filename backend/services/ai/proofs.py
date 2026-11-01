"""Finite predicates over typed facts and source classes."""
from .protocol import ProtocolError

PREDICATES = {
    "engine_result": ({"engine_score", "count"}, {"customer_record", "scoring", "history"}),
    "has_record": ({"enum", "raw_scalar", "person_list"}, {"customer_record"}),
    "missing_record": ({"enum", "raw_scalar", "person_list"}, {"customer_record"}),
    "count_from_complete_list": ({"count"}, {"customer_record"}),
    "trend_all_steps": ({"trend"}, {"history"}),
    "quoted_source": ({"attributed_quote"}, {"general_knowledge", "reviewed_policy"}),
    "record_state_union": ({"enum", "raw_scalar", "person_list"}, {"customer_record"}),
    "retrieval_state": ({"count"}, {"general_knowledge"}),
}


def prove(node, package):
    predicate = PREDICATES.get(node["predicate_id"])
    if not predicate or not node["fact_refs"]:
        return False
    kinds, classes = predicate
    for ref in node["fact_refs"]:
        fact = package["facts"].get(ref)
        if not fact or fact["kind"] not in kinds or fact["subject_ref"] not in package["snapshot"]["members"]:
            return False
        if any(s not in package["sources"] or package["sources"][s]["source_class"] not in classes for s in fact["source_refs"]):
            return False
        if node["predicate_id"] == "has_record" and fact["state"] != "known":
            return False
        if node["predicate_id"] == "missing_record" and fact["state"] != "missing":
            return False
        if node["predicate_id"] == "count_from_complete_list":
            inputs = fact["derivation"]["fact_refs"]
            if not inputs or any(package["facts"][f]["kind"] != "person_list" or not package["facts"][f]["value"]["complete"] for f in inputs):
                return False
    return True


def fact_closure(package, refs, visiting=None):
    visiting = set() if visiting is None else visiting
    closure = set()
    for ref in refs:
        if ref in visiting or ref not in package["facts"]:
            raise ProtocolError("cyclic_or_missing_fact")
        closure.add(ref)
        closure.update(fact_closure(package, package["facts"][ref]["derivation"]["fact_refs"], visiting | {ref}))
    return closure
