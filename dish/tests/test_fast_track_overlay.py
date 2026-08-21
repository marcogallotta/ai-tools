from __future__ import annotations
import importlib.util, json
from datetime import datetime, timezone
from pathlib import Path
import pytest

DISH_ROOT=Path(__file__).resolve().parents[1]
REPO_ROOT=DISH_ROOT.parent
SCRIPT=DISH_ROOT/'scripts'/'chatgpt_project_kernels.py'
SPEC=importlib.util.spec_from_file_location('chatgpt_project_kernels_fast_track',SCRIPT); assert SPEC and SPEC.loader
kernels=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(kernels)

GATE_DIGEST='sha256:bbcf3768f1f0b0944a3c025cbd14f9c411787f029484bc4538ddac14a911a78c'

def _overlay(**changes):
    value={'version':'fasttrack-r3','state':'ACTIVE','generation':'g-2026-08-18','scope':['repository-context-bundle-witness@1'],'gate_semantics':{'repository-context-bundle-witness@1':GATE_DIGEST},'expiry':None,'reason':'avoid repeated repository-bundle transport waivers'}
    value.update(changes); return value

def _use(value=None,**changes):
    args={'gate_id':'repository-context-bundle-witness','gate_version':1,'task':'1217594495187308','candidate':'PR#169@abc','action':'implementation repository-context admission','raw_evidence':'FAILED: exact repository bundle unavailable through connector transport','now':datetime(2026,8,18,tzinfo=timezone.utc)}
    args.update(changes)
    return kernels.fast_track_use(_overlay() if value is None else value,**args)

def test_active_overlay_applies_exact_current_gate_and_records_exact_use():
    result=_use()
    assert result['marker']=='GATE WAIVED BY MARCO OVERRIDE'
    assert result['overlay_generation']=='g-2026-08-18'
    assert result['overlay_digest'].startswith('sha256:') and len(result['overlay_digest'])==71
    assert result['gate_id']=='repository-context-bundle-witness' and result['gate_version']==1
    assert result['gate_semantic_digest']==GATE_DIGEST
    assert result['task']=='1217594495187308' and result['candidate']=='PR#169@abc'
    assert result['raw_evidence'].startswith('FAILED:')

def test_overlay_digest_is_semantic_and_stable_across_formatting_and_optional_reason():
    value=_overlay(scope=['repository-context-bundle-witness@1','repository-context-bundle-witness@1'],reason='')
    block='prefix text\nMARCO OVERRIDE — FAST-TRACK PROCESS\n'+json.dumps(value,indent=4,sort_keys=False)+'\nremaining project text'
    parsed=kernels.parse_fast_track_overlay_block(block)
    assert parsed['scope']==['repository-context-bundle-witness@1'] and parsed['reason']==''
    assert kernels.fast_track_overlay_digest(parsed)==kernels.fast_track_overlay_digest(_overlay(reason=''))

def test_wildcard_unknown_and_future_gate_scope_are_not_inherited():
    with pytest.raises(kernels.KernelError,match='invalid fast-track overlay fields'):
        kernels.canonical_fast_track_overlay(_overlay(scope=['*@1']))
    with pytest.raises(kernels.KernelError,match='unknown, stale, or materially changed'):
        _use(_overlay(scope=['repository-context-bundle-witness@2'],gate_semantics={'repository-context-bundle-witness@2':GATE_DIGEST}),gate_version=2)
    with pytest.raises(kernels.KernelError,match='outside captured overlay scope'):
        _use(_overlay(scope=['other-gate@1'],gate_semantics={'other-gate@1':GATE_DIGEST}))

def test_material_gate_change_requires_new_current_version(tmp_path,monkeypatch):
    current=json.loads((DISH_ROOT/'docs'/'chatgpt-projects'/'fast-track-gates.json').read_text())
    gate=current['gates'][0]
    v2={'waives':['different material waiver'],'retains':gate['versions']['1']['retains']}
    semantic={'id':gate['id'],'version':2,'waives':v2['waives'],'retains':v2['retains']}
    v2['semantic_digest']='sha256:'+kernels._semantic_json_hash(semantic)
    gate['current_version']=2; gate['versions']['2']=v2
    path=tmp_path/'gates.json'; path.write_text(json.dumps(current))
    monkeypatch.setattr(kernels,'FAST_TRACK_GATE_REGISTRY_PATH',path)
    with pytest.raises(kernels.KernelError,match='unknown, stale, or materially changed'):
        _use()


def test_same_version_semantic_rewrite_with_recomputed_registry_digest_is_rejected(tmp_path,monkeypatch):
    current=json.loads((DISH_ROOT/'docs'/'chatgpt-projects'/'fast-track-gates.json').read_text())
    gate=current['gates'][0]
    v1=gate['versions']['1']
    v1['waives']=['materially broader waiver under the old version number']
    semantic={'id':gate['id'],'version':1,'waives':v1['waives'],'retains':v1['retains']}
    v1['semantic_digest']='sha256:'+kernels._semantic_json_hash(semantic)
    path=tmp_path/'gates.json'; path.write_text(json.dumps(current))
    monkeypatch.setattr(kernels,'FAST_TRACK_GATE_REGISTRY_PATH',path)
    with pytest.raises(kernels.KernelError,match='unknown, stale, or materially changed'):
        _use()


def test_updated_gate_version_requires_and_accepts_updated_overlay_semantic_binding(tmp_path,monkeypatch):
    current=json.loads((DISH_ROOT/'docs'/'chatgpt-projects'/'fast-track-gates.json').read_text())
    gate=current['gates'][0]
    v2={'waives':['different material waiver'],'retains':gate['versions']['1']['retains']}
    semantic={'id':gate['id'],'version':2,'waives':v2['waives'],'retains':v2['retains']}
    v2['semantic_digest']='sha256:'+kernels._semantic_json_hash(semantic)
    gate['current_version']=2; gate['versions']['2']=v2
    path=tmp_path/'gates.json'; path.write_text(json.dumps(current))
    monkeypatch.setattr(kernels,'FAST_TRACK_GATE_REGISTRY_PATH',path)
    result=_use(_overlay(scope=['repository-context-bundle-witness@2'],gate_semantics={'repository-context-bundle-witness@2':v2['semantic_digest']}),gate_version=2)
    assert result['gate_semantic_digest']==v2['semantic_digest']


def test_digestless_gate_version_scope_is_not_persistent_authority():
    value=_overlay(); value.pop('gate_semantics')
    with pytest.raises(kernels.KernelError,match='invalid fast-track overlay fields'):
        kernels.canonical_fast_track_overlay(value)

def test_raw_failure_stays_failed_and_downstream_record_is_self_contained():
    result=_use()
    assert result['raw_evidence'].startswith('FAILED:')
    assert set(('overlay_generation','overlay_digest','gate_id','gate_version','gate_semantic_digest','task','candidate','action','raw_evidence')) <= set(result)

def test_inactive_and_expired_generations_cannot_be_used():
    with pytest.raises(kernels.KernelError,match='inactive'):
        _use(_overlay(state='INACTIVE'))
    with pytest.raises(kernels.KernelError,match='expired'):
        _use(_overlay(expiry='2026-08-18T10:00:00+00:00'),now=datetime(2026,8,18,11,0,tzinfo=timezone.utc))

def test_registry_retains_review_integration_identity_and_safety_boundaries():
    gate=kernels.fast_track_gate_registry()['repository-context-bundle-witness']
    retained=' | '.join(gate['retains'])
    for phrase in ('exact task/branch/PR/head identity','independent semantic Review','Integration separation','production/destructive-operation safeguards','genuine platform/system impossibilities','wrong-SHA bundle rejection'):
        assert phrase in retained

def test_project_policy_separates_overlay_freshness_from_repository_reground():
    root=(REPO_ROOT/'CLAUDE.md').read_text()
    doc=(DISH_ROOT/'docs'/'agents'/'fast-track-process.md').read_text()
    assert 'ordinary compaction/re-ground does not refresh Project settings' in root
    assert 'Ordinary in-session compaction or repository re-ground does **not** refresh' in doc
    assert 'Current-chat Marco change/revocation is immediate' in root
    assert 'Current-chat Marco change/revocation is immediate' in doc
    assert 'later verified new Project chat/session captures the then-current Project settings' in doc
    assert 'future live Project-settings refresh primitive' in doc

def test_generated_kernels_carry_direct_overlay_rule_and_triggered_procedure():
    manifest,source=kernels.load_canonical()
    for role in source['roles']:
        rendered=kernels.render_role(manifest,source,role)
        assert 'Fast-track: read triggered Procedure.' in rendered
        assert 'fast-track' in rendered
        assert 'dish/docs/agents/fast-track-process.md#Procedure' in rendered


def _worker_profile():
    manifest,source=kernels.load_canonical()
    return kernels.generated_profile_paths(manifest,source)['worker'].read_text()


def test_manual_worker_profile_size_and_exact_modes():
    profile=_worker_profile()
    assert len(profile) <= 8000
    assert 'Exactly one semantic mode is active at a time: **Implementation**, **Code Review**, **Design Review**, or **Audit**.' in profile
    assert 'Integration/merge/deploy/cutover are outside Worker.' in profile
    assert 'not a ninth semantic role' in profile


def test_manual_worker_profile_requires_same_worker_block_fix_without_automated_provenance():
    profile=_worker_profile()
    assert 'same Worker MUST explicitly switch to Implementation' in profile
    assert 'Without another Marco prompt' in profile
    assert 'does **not** require Workspace-Agent launch' in profile
    assert 'their absence never gates the ordinary manual Project-chat path' in profile
    assert 'fresh Worker performs the next Review' in profile


def test_manual_worker_profile_preserves_memory_based_no_self_review():
    profile=_worker_profile()
    assert 'may not independently Review that head while it remembers or can recover that authorship' in profile
    assert 'Genuine later compaction/forgetting follows Marco' in profile
    assert 'no durable chat-taint/provenance machinery' in profile


def test_manual_worker_design_review_stays_exact_snapshot_bound():
    profile=_worker_profile()
    for phrase in (
        'SHA-256 of exact canonical task notes/design snapshot',
        'immediately before publishing `VERDICT: PASS` or `VERDICT: BLOCK`, reread the canonical task',
        'publish no verdict for the new candidate',
        'Chat-only verdict does not count.',
    ):
        assert phrase in profile
