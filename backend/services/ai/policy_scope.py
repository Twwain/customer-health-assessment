"""Frozen SQL-evidence identities shared by intent guidance and validation."""
from copy import deepcopy
import re

from .protocol import ProtocolError, fingerprint


def source_ref(evidence):
    return 'src:'+str(evidence['chunk_id'])


def normalize_evidence(evidence):
    """Normalize before grouping/hash; never choose among conflicting rows."""
    unique = {}
    for row in evidence:
        if not isinstance(row, dict):
            raise ProtocolError('source_changed', '/evidence')
        if any(type(row.get(k)) is not int or row[k] <= 0 for k in ('document_id', 'item_id', 'chunk_id')):
            raise ProtocolError('source_changed', '/evidence')
        if not isinstance(row.get('title'), str) or not isinstance(row.get('content_hash'), str):
            raise ProtocolError('source_changed', '/evidence')
        if not re.fullmatch(r'[0-9a-f]{64}', row['content_hash']):
            raise ProtocolError('source_changed', '/evidence')
        ref = source_ref(row)
        # Content/title/category are frozen metadata, not identity aliases.
        # Hash equality must not silently forgive conflicting captured rows.
        identity = {k: row.get(k) for k in ('document_id', 'item_id', 'chunk_id', 'content_hash',
                    'title', 'category', 'content', 'source_class', 'applicability')}
        if ref in unique and unique[ref][0] != identity:
            raise ProtocolError('source_changed', '/evidence')
        unique[ref] = (identity, deepcopy(row))
    return [unique[ref][1] for ref in sorted(unique)]


def document_groups(evidence):
    by_document = {}
    for row in normalize_evidence(evidence):
        by_document.setdefault(row['document_id'], []).append(row)
    groups = []
    for did, rows in sorted(by_document.items()):
        groups.append(dict(document_ref='knowledge_document:'+str(did),
            titles=sorted({r['title'] for r in rows}),
            evidence_revision=fingerprint(sorted((r['document_id'], r['item_id'], source_ref(r), r['content_hash']) for r in rows)),
            source_refs=sorted({source_ref(r) for r in rows})))
    return groups


def resolve_policy_scope(question, evidence):
    """Book-title entities bind identity; they do not force a policy intent."""
    evidence = normalize_evidence(evidence)
    groups = document_groups(evidence)
    titles = re.findall(r'《([^《》\r\n]{1,200})》', question)
    if not titles:
        status = 'general_hits' if evidence else 'general_no_hits'
        selected = {e['document_id'] for e in evidence}
    else:
        matches = [{e['document_id'] for e in evidence if e['title'] == title} for title in titles]
        status = ('named_missing' if any(not m for m in matches) else
                  'named_ambiguous' if any(len(m) > 1 for m in matches) else 'named_unique')
        selected = set.union(*matches) if status == 'named_unique' else set()
    return dict(version='1.0', status=status,
        document_refs=['knowledge_document:'+str(d) for d in sorted(selected)],
        allowed_source_refs=sorted({source_ref(e) for e in evidence if e['document_id'] in selected}),
        document_groups=groups)


def freeze_policy_scope(scope, evidence, selected_refs):
    """Private contract identity contains no retrieved titles or source text."""
    evidence = normalize_evidence(evidence)
    selected = set(selected_refs)
    by_ref = {source_ref(e): e for e in evidence}
    if not selected.issubset(by_ref) or not selected.issubset(scope['allowed_source_refs']):
        raise ProtocolError('invalid_intent_reference', '/intents/source_refs', scope['allowed_source_refs'])
    identities = sorted({'knowledge_document:'+str(by_ref[s]['document_id']) for s in selected})
    groups = {g['document_ref']: g for g in document_groups(evidence)}
    return dict(version='1.0', document_refs=identities,
        document_revisions={d: groups[d]['evidence_revision'] for d in identities},
        source_bindings={s: dict(document_ref='knowledge_document:'+str(by_ref[s]['document_id']),
            item_ref='knowledge_item:'+str(by_ref[s]['item_id']), content_hash=by_ref[s]['content_hash']) for s in sorted(selected)})


def rebind_policy_scope(base, package):
    """Preserve the chosen identities, never re-resolve an old title."""
    if base.get('version') != '2.4' or not isinstance(base.get('policy_scope'), dict):
        raise ProtocolError('needs_clarification', '/intents/source_refs')
    saved = base['policy_scope']
    if saved.get('version') != '1.0' or not isinstance(saved.get('source_bindings'), dict):
        raise ProtocolError('needs_clarification', '/intents/source_refs')
    if not isinstance(saved.get('document_refs'), list) or not isinstance(saved.get('document_revisions'), dict):
        raise ProtocolError('needs_clarification', '/intents/source_refs')
    if any(not isinstance(ref, str) or not isinstance(binding, dict)
           or set(binding) != {'document_ref', 'item_ref', 'content_hash'}
           or any(not isinstance(v, str) for v in binding.values())
           for ref, binding in saved['source_bindings'].items()):
        raise ProtocolError('needs_clarification', '/intents/source_refs')
    evidence = normalize_evidence(package['evidence'])
    by_ref = {source_ref(e): e for e in evidence}
    selected = set(saved['source_bindings'])
    current_groups = {g['document_ref']: g for g in document_groups(evidence)}
    if not selected and evidence:
        raise ProtocolError('source_changed', '/intents/source_refs')
    for ref, old in saved['source_bindings'].items():
        row = by_ref.get(ref)
        if row is None or old != dict(document_ref='knowledge_document:'+str(row['document_id']),
                item_ref='knowledge_item:'+str(row['item_id']), content_hash=row['content_hash']):
            raise ProtocolError('source_changed', '/intents/source_refs')
    expected_documents = sorted({v['document_ref'] for v in saved['source_bindings'].values()})
    if saved.get('document_refs') != expected_documents or set(saved.get('document_revisions', {})) != set(expected_documents):
        raise ProtocolError('needs_clarification', '/intents/source_refs')
    for ref, revision in saved['document_revisions'].items():
        if ref not in current_groups or current_groups[ref]['evidence_revision'] != revision:
            raise ProtocolError('source_changed', '/intents/source_refs')
    # Chosen documents stay chosen even when another same-title document is new.
    return dict(version='1.0', status='named_unique' if selected else 'general_no_hits',
        document_refs=expected_documents, allowed_source_refs=sorted(selected),
        document_groups=[current_groups[d] for d in expected_documents])
