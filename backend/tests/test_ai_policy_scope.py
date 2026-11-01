"""A2 source identity oracles: synthetic data only, no provider calls."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from test_ai_protocol import v2db
from models import KnowledgeDocument, KnowledgeItem, KnowledgeChunk
from services.ai.protocol import ProtocolError, fingerprint, load_runtime_assets
from services.ai.policy_scope import (normalize_evidence, resolve_policy_scope,
                                       freeze_policy_scope, rebind_policy_scope)
from services.ai.intent_contract import compile_schema, errors, rebind_intents
from services.ai.facts import capture
from services.ai.catalog import build_catalog
from services.ai.contracts import build_contract


def row(chunk=71, document=71, item=71, title='核验制度', content='财务负责人36小时内核验。'):
    return dict(chunk_id=chunk, document_id=document, item_id=item, title=title, category='内部规范',
        content=content, source_class='general_knowledge', applicability='unknown',
        content_hash=fingerprint(dict(content=content,title=title,category='内部规范',status='canonical',document_status='canonical')))


def with_evidence(v2db, evidence):
    db, customer, _ = v2db
    package = capture(db.get_bind(), customer.id)
    package['evidence'] = evidence
    package['retrieval_status'] = 'available' if evidence else 'no_hits'
    return package


def policy(package, refs):
    return dict(kind='policy',subject_refs=[package['customer_ref']],field_refs=[],option_refs=[],source_refs=refs,count=None)


def response(*intents):
    return dict(version='2.4',status='resolved',clarification_code=None,intents=list(intents))


def test_document_scope_deduplicates_before_revision_and_ignores_retrieval_order():
    first=row(); second=row(chunk=72)
    expected=resolve_policy_scope('请解释《核验制度》。',[first,second])
    assert expected['status']=='named_unique'
    assert expected['document_refs']==['knowledge_document:71']
    assert expected['allowed_source_refs']==['src:71','src:72']
    assert resolve_policy_scope('请解释《核验制度》。',[second,first,first,second])==expected
    assert normalize_evidence([second,first,first])==[first,second]


@pytest.mark.parametrize('change',[
    {'document_id':72},{'item_id':72},{'content_hash':'9'*64},{'title':'另一标题'},
    {'content':'内部一致性破坏'},{'category':'另一分类'},{'source_class':'reviewed_policy'}])
def test_same_source_with_conflicting_identity_never_produces_scope(change):
    original=row()
    with pytest.raises(ProtocolError,match='source_changed'):
        resolve_policy_scope('制度查询',[original,dict(original,**change)])


@pytest.mark.parametrize('same_content',[False,True])
def test_different_documents_same_title_are_ambiguous_even_with_identical_text(v2db,same_content):
    one=row(); other=row(chunk=72,document=72,item=72,content=one['content'] if same_content else '销售负责人999小时内核验。')
    package=with_evidence(v2db,[one,other])
    scope=resolve_policy_scope('请仅按《核验制度》引用原文。',package['evidence'])
    assert scope['status']=='named_ambiguous' and scope['allowed_source_refs']==[]
    schema=compile_schema(package,scope=scope)
    variants=schema['oneOf'][0]['properties']['intents']['items']['oneOf']
    assert 'policy' not in {v['properties']['kind']['const'] for v in variants}
    invalid=response(policy(package,['src:71','src:72']))
    assert not Draft202012Validator(schema).is_valid(invalid)
    assert errors(invalid,package,scope=scope)[0].code=='needs_clarification'
    # The source entity parser is not an intent keyword router.
    score=dict(policy(package,[]),kind='score_grade')
    assert not errors(response(score),package,scope=scope)
    clarification=dict(version='2.4',status='clarify',clarification_code='choose_source',intents=[])
    assert not errors(clarification,package,scope=scope)


def test_named_missing_is_not_generic_no_hits_and_multiple_named_documents_are_not_partial(v2db):
    package=with_evidence(v2db,[])
    assert not errors(response(policy(package,[])),package,'请解释制度的责任主体。')
    assert errors(response(policy(package,[])),package,'请引用《未召回制度》。')[0].code=='needs_clarification'
    package['evidence']=[row(),row(chunk=72,document=72,item=72,title='排期制度')]
    scope=resolve_policy_scope('请引用《核验制度》和《排期制度》。',package['evidence'])
    assert scope['status']=='named_unique'
    assert not errors(response(policy(package,['src:71','src:72'])),package,scope=scope)
    assert errors(response(policy(package,['src:71'])),package,scope=scope)
    missing=resolve_policy_scope('请引用《核验制度》和《缺失制度》。',package['evidence'])
    assert missing['status']=='named_missing' and missing['allowed_source_refs']==[]


def test_generic_hits_allow_selected_subset_and_preserve_document_aliases(v2db):
    evidence=[row(),row(chunk=72,item=72,title='核验制度补充'),row(chunk=73,document=73,item=73,title='其他制度')]
    package=with_evidence(v2db,evidence)
    generic=resolve_policy_scope('请引用相关制度。',evidence)
    assert not errors(response(policy(package,['src:71'])),package,scope=generic)
    named=resolve_policy_scope('请引用《核验制度补充》。',evidence)
    assert named['document_refs']==['knowledge_document:71']
    assert named['allowed_source_refs']==['src:71','src:72']
    assert named['document_groups'][0]['titles']==['核验制度','核验制度补充']
    assert errors(response(policy(package,['src:72'])),package,scope=named)


def test_dlp_title_collision_cannot_merge_document_identity(v2db):
    from services.ai.guardrails import sanitize_for_outbound
    one=row(title='制度13812345678')
    other=row(chunk=72,document=72,item=72,title='制度13899995678')
    assert sanitize_for_outbound(one['title'])[0]==sanitize_for_outbound(other['title'])[0]
    package=with_evidence(v2db,[one,other])
    scope=resolve_policy_scope('请引用《制度13812345678》。',package['evidence'])
    assert scope['status']=='named_unique' and scope['allowed_source_refs']==['src:71']
    assert not errors(response(policy(package,['src:71'])),package,scope=scope)
    assert errors(response(policy(package,['src:72'])),package,scope=scope)


def test_contract_persists_identity_without_source_text_and_regenerates_same_selection(v2db):
    package=with_evidence(v2db,[row()])
    catalog=build_catalog(package)
    scope=resolve_policy_scope('请引用《核验制度》。',package['evidence'])
    base=build_contract(package,catalog,'free_qa',intents=[policy(package,['src:71'])],policy_scope=scope,request_hash='original')
    assert base['version']=='2.4'
    assert base['policy_scope']==dict(version='1.0',document_refs=['knowledge_document:71'],
        document_revisions={'knowledge_document:71':scope['document_groups'][0]['evidence_revision']},
        source_bindings={'src:71':dict(document_ref='knowledge_document:71',item_ref='knowledge_item:71',content_hash=row()['content_hash'])})
    assert '核验制度' not in str(base['policy_scope']) and '财务负责人' not in str(base['policy_scope'])
    # A newly retrieved same-title document cannot change an already chosen identity.
    changed=deepcopy(package);changed['evidence'].append(row(chunk=72,document=72,item=72))
    assert resolve_policy_scope('《核验制度》',changed['evidence'])['status']=='named_ambiguous'
    rebound=build_contract(changed,build_catalog(changed),'free_qa',base_contract=base)
    assert rebound['requirements']['required_policy_sources']==['src:71']
    assert rebound['policy_scope']==base['policy_scope']
    assert rebound['request_hash']=='original' and rebound['base_contract_ref']==base['id']
    assert 'r:knowledge72' not in rebound['allowed_catalog_ids']


@pytest.mark.parametrize('change',[
    {'document_id':72},{'item_id':72},{'content_hash':'9'*64}])
def test_new_contract_regeneration_rejects_selected_identity_or_content_change(v2db,change):
    package=with_evidence(v2db,[row()])
    catalog=build_catalog(package)
    base=build_contract(package,catalog,'free_qa',intents=[policy(package,['src:71'])])
    changed=deepcopy(package);changed['evidence'][0].update(change)
    with pytest.raises(ProtocolError,match='source_changed'):
        rebind_intents(base,changed)
    changed=deepcopy(package);changed['evidence']=[]
    with pytest.raises(ProtocolError,match='source_changed'):
        rebind_intents(base,changed)


def test_regeneration_rejects_old_contracts_and_missing_current_fields(v2db):
    package=with_evidence(v2db,[row()]);catalog=build_catalog(package)
    base=build_contract(package,catalog,'free_qa',intents=[policy(package,['src:71'])])
    for version in ['2.1','2.2','2.3']:
        with pytest.raises(ProtocolError,match='needs_clarification'):
            rebind_intents(dict(base,version=version),package)
    score=dict(policy(package,[]),kind='score_grade')
    old=build_contract(package,catalog,'free_qa',intents=[score]);old['version']='2.1'
    del old['intents'][0]['source_refs']
    with pytest.raises(ProtocolError,match='needs_clarification'):
        rebind_intents(old,package)
    old['version']='2.4'
    with pytest.raises(ProtocolError):
        rebind_intents(old,package)
    malformed=deepcopy(base);malformed['policy_scope']['document_revisions']=None
    with pytest.raises(ProtocolError,match='needs_clarification'):
        rebind_intents(malformed,package)


@pytest.mark.parametrize('scenario', ['strategy', 'alert', 'alert_analysis'])
def test_removed_scenarios_cannot_create_contracts(v2db, scenario):
    package=with_evidence(v2db,[])
    with pytest.raises(ProtocolError,match='unsupported_scenario'):
        build_contract(package,build_catalog(package),scenario)


def test_old_assessment_contract_cannot_be_regenerated(v2db):
    package=with_evidence(v2db,[]);catalog=build_catalog(package)
    base=build_contract(package,catalog,'assessment')
    base['version']='2.3'
    with pytest.raises(ProtocolError,match='needs_clarification'):
        build_contract(package,catalog,'assessment',base_contract=base)


def test_no_hit_contract_cannot_implicitly_select_new_sources(v2db):
    package=with_evidence(v2db,[]);catalog=build_catalog(package)
    base=build_contract(package,catalog,'free_qa',intents=[policy(package,[])])
    assert rebind_policy_scope(base,package)['status']=='general_no_hits'
    changed=deepcopy(package);changed['evidence']=[row()]
    with pytest.raises(ProtocolError,match='source_changed'):
        rebind_intents(base,changed)


def test_all_compilers_use_supplied_request_asset_bundle_without_rereading(v2db,monkeypatch):
    from services.ai import protocol
    bundle=load_runtime_assets()
    package=with_evidence(v2db,[row()]);scope=resolve_policy_scope('《核验制度》',package['evidence'])
    monkeypatch.setattr(protocol,'load_runtime_assets',lambda:(_ for _ in ()).throw(AssertionError('Unexpected asset reread')))
    value=policy(package,['src:71'])
    assert Draft202012Validator(compile_schema(package,runtime_assets=bundle,scope=scope)).is_valid(response(value))
    assert not errors(response(value),package,runtime_assets=bundle,scope=scope)
    base=build_contract(package,build_catalog(package),'free_qa',intents=[value],runtime_assets=bundle,policy_scope=scope)
    assert rebind_intents(base,package,runtime_assets=bundle)==[value]


def test_real_retrieval_identity_fingerprint_is_order_and_duplicate_stable(v2db,monkeypatch):
    from services.ai import evidence
    db,customer,_=v2db
    doc=KnowledgeDocument(title='合成制度',category='内部规范',status='canonical',index_status='indexed')
    db.add(doc);db.commit()
    item=KnowledgeItem(document_id=doc.id,title=doc.title,category=doc.category,status='canonical')
    db.add(item);db.commit()
    chunks=[KnowledgeChunk(document_id=doc.id,chunk_index=i,content=text)
            for i,text in enumerate(['财务负责人36小时核验。','财务负责人72小时取得排期。'])]
    db.add_all(chunks);db.commit()
    hits=[SimpleNamespace(document_id=doc.id,item_id=item.id,chunk_index=c.chunk_index) for c in chunks]
    class Retriever:
        def retrieve(self,*args,**kwargs):return list(hits)
    monkeypatch.setattr(evidence,'KnowledgeRetriever',Retriever)
    first=capture(db.get_bind(),customer.id)
    evidence.retrieve(db,first,'合成制度',customer)
    hits[:]=[hits[1],hits[0],hits[0]]
    second=capture(db.get_bind(),customer.id)
    evidence.retrieve(db,second,'合成制度',customer)
    assert first['evidence']==second['evidence']
    assert first['source_fingerprint']==second['source_fingerprint']
    assert first['source_fingerprint']==fingerprint([first['input_fingerprint'],[
        dict(document_id=e['document_id'],item_id=e['item_id'],chunk_id=e['chunk_id'],content_hash=e['content_hash']) for e in first['evidence']]])
    other_doc=KnowledgeDocument(title='其他制度',category='内部规范',status='canonical',index_status='indexed')
    db.add(other_doc);db.commit()
    wrong_item=KnowledgeItem(document_id=other_doc.id,title=doc.title,category=doc.category,status='canonical')
    db.add(wrong_item);db.commit()
    hits[:]=[SimpleNamespace(document_id=doc.id,item_id=wrong_item.id,chunk_index=chunks[0].chunk_index)]
    with pytest.raises(ProtocolError,match='source_changed'):
        evidence.retrieve(db,capture(db.get_bind(),customer.id),'合成制度',customer)
