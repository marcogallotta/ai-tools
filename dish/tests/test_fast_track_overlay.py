from __future__ import annotations
import copy, importlib.util, json
from datetime import datetime, timezone
from pathlib import Path
import pytest

DISH_ROOT=Path(__file__).resolve().parents[1]
REPO_ROOT=DISH_ROOT.parent
SCRIPT=DISH_ROOT/'scripts'/'chatgpt_project_kernels.py'
SPEC=importlib.util.spec_from_file_location('chatgpt_project_kernels_fast_track',SCRIPT); assert SPEC and SPEC.loader
kernels=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(kernels)

GATE_ID='repository-context-bundle-witness'
GATE_DIGEST='sha256:bbcf3768f1f0b0944a3c025cbd14f9c411787f029484bc4538ddac14a911a78c'

def _activation(aid='activation-2026-08-23',task='1217999000000001',day='2026-08-23',**follow_changes):
    follow={'task_gid':task,'project_gid':kernels.DEVELOPMENT_WORKFLOW_PROJECT_GID,'priority':'P-CRITICAL','due_on':day,'read_back_at':f'{day}T10:05:00+00:00','evidence_ref':f'asana:task:{task}@readback','gate_ref':f'{GATE_ID}@1:{GATE_DIGEST}','marco_decision_ref':'asana:task:1217599491860900#story:1217747798724121','objective':'remove-or-narrow-or-replace-with-source-fix'}
    follow.update(follow_changes)
    return {'activation_id':aid,'activated_on':day,'follow_up':follow}

def _exception(**changes):
    value={'gate_id':GATE_ID,'gate_version':1,'gate_semantic_digest':GATE_DIGEST,'state':'ACTIVE','expiry':None,'condition':None,'marco_decision':{'task_gid':'1217599491860900','story_gid':'1217747798724121','decided_at':'2026-08-22T19:02:00+00:00','decision':'Approve exact standing exception and same-day P-CRITICAL safeguard.'},'activations':[_activation()],'current_activation_id':'activation-2026-08-23'}
    value.update(changes); return value

def _registry(exception=True):
    value=json.loads((DISH_ROOT/'docs'/'chatgpt-projects'/'fast-track-gates.json').read_text())
    value['exceptions']=[_exception()] if exception else []
    return value

def _bind(tmp_path,monkeypatch,value):
    path=tmp_path/'gates.json'; path.write_text(json.dumps(value))
    monkeypatch.setattr(kernels,'FAST_TRACK_GATE_REGISTRY_PATH',path)

def _use(**changes):
    args={'gate_id':GATE_ID,'gate_version':1,'task':'1217594495187308','candidate':'PR#169@abc','action':'implementation repository-context admission','raw_evidence':'FAILED: exact repository bundle unavailable through connector transport','now':datetime(2026,8,23,tzinfo=timezone.utc)}
    args.update(changes); return kernels.fast_track_use(**args)

def test_active_repository_exception_applies_exact_gate_and_records_self_contained_use(tmp_path,monkeypatch):
    _bind(tmp_path,monkeypatch,_registry())
    result=_use()
    assert result['marker']=='GATE WAIVED BY MARCO OVERRIDE'
    assert result['registry_version']=='fasttrack-r4'
    assert result['gate_id']==GATE_ID and result['gate_version']==1
    assert result['gate_semantic_digest']==GATE_DIGEST
    assert result['activation_id']=='activation-2026-08-23'
    assert result['follow_up_task_gid']=='1217999000000001'
    assert result['marco_decision']['story_gid']=='1217747798724121'
    assert result['raw_evidence'].startswith('FAILED:')

def test_absent_exception_falls_back_to_ordinary_gate(tmp_path,monkeypatch):
    _bind(tmp_path,monkeypatch,_registry(exception=False))
    with pytest.raises(kernels.KernelError,match='no active standing exception'):
        _use()

@pytest.mark.parametrize(('field','value'),[
    ('project_gid','wrong-project'),('priority','P0'),('due_on','2026-08-24'),
    ('gate_ref','wrong-gate'),('marco_decision_ref','wrong-decision'),('objective','keep-forever'),
])
def test_activation_is_invalid_without_same_day_pcritical_canonical_followup_readback(tmp_path,monkeypatch,field,value):
    registry=_registry(); registry['exceptions'][0]['activations']=[_activation(**{field:value})]
    _bind(tmp_path,monkeypatch,registry)
    with pytest.raises(kernels.KernelError,match='same-day P-CRITICAL Development Workflow follow-up'):
        _use()

def test_activation_followup_must_be_read_back_on_activation_day(tmp_path,monkeypatch):
    registry=_registry(); registry['exceptions'][0]['activations']=[_activation(read_back_at='2026-08-24T00:01:00+00:00')]
    _bind(tmp_path,monkeypatch,registry)
    with pytest.raises(kernels.KernelError,match='read back on activation day'):
        _use()

def test_repeated_use_does_not_create_debt_and_reactivation_binds_newest_followup(tmp_path,monkeypatch):
    registry=_registry(); old=_activation('activation-1','1217999000000001','2026-08-22')
    new=_activation('activation-2','1217999000000002','2026-08-23')
    registry['exceptions'][0].update(activations=[old,new],current_activation_id='activation-2')
    _bind(tmp_path,monkeypatch,registry)
    first=_use(); second=_use()
    assert first['activation_id']==second['activation_id']=='activation-2'
    assert first['follow_up_task_gid']==second['follow_up_task_gid']=='1217999000000002'
    stale=copy.deepcopy(registry); stale['exceptions'][0]['current_activation_id']='activation-1'
    _bind(tmp_path,monkeypatch,stale)
    with pytest.raises(kernels.KernelError,match='newest activation'):
        _use()

def test_unknown_future_or_materially_changed_gate_is_not_inherited(tmp_path,monkeypatch):
    registry=_registry(); gate=registry['gates'][0]
    v2={'waives':['different material waiver'],'retains':gate['versions']['1']['retains']}
    semantic={'id':gate['id'],'version':2,'waives':v2['waives'],'retains':v2['retains']}
    v2['semantic_digest']='sha256:'+kernels._semantic_json_hash(semantic)
    gate['current_version']=2; gate['versions']['2']=v2
    _bind(tmp_path,monkeypatch,registry)
    with pytest.raises(kernels.KernelError,match='unknown, stale, or materially changed'):
        _use()
    with pytest.raises(kernels.KernelError,match='unknown, stale, or materially changed'):
        _use(gate_id='future-gate')

def test_same_version_semantic_rewrite_cannot_expand_existing_exception(tmp_path,monkeypatch):
    registry=_registry(); v1=registry['gates'][0]['versions']['1']
    v1['waives']=['materially broader waiver under old version']
    semantic={'id':GATE_ID,'version':1,'waives':v1['waives'],'retains':v1['retains']}
    v1['semantic_digest']='sha256:'+kernels._semantic_json_hash(semantic)
    _bind(tmp_path,monkeypatch,registry)
    with pytest.raises(kernels.KernelError,match='unknown, stale, or materially changed'):
        _use()

def test_inactive_expired_revoked_and_unproved_condition_cannot_be_used(tmp_path,monkeypatch):
    registry=_registry(); registry['exceptions'][0].update(state='INACTIVE',current_activation_id=None)
    _bind(tmp_path,monkeypatch,registry)
    with pytest.raises(kernels.KernelError,match='inactive'): _use()
    registry=_registry(); registry['exceptions'][0]['expiry']='2026-08-22T23:59:00+00:00'
    _bind(tmp_path,monkeypatch,registry)
    with pytest.raises(kernels.KernelError,match='expired'): _use()
    registry=_registry(); _bind(tmp_path,monkeypatch,registry)
    with pytest.raises(kernels.KernelError,match='revoked in the current chat'): _use(current_chat_revoked=True)
    registry['exceptions'][0]['condition']='bundle transport is unavailable'; _bind(tmp_path,monkeypatch,registry)
    with pytest.raises(kernels.KernelError,match='condition requires current evidence'): _use()
    assert _use(condition_evidence='connector returned no exact artifact')['condition_evidence']

def test_registry_requires_exact_marco_provenance_and_retains_boundaries(tmp_path,monkeypatch):
    registry=_registry(); registry['exceptions'][0]['marco_decision']['story_gid']=''
    _bind(tmp_path,monkeypatch,registry)
    with pytest.raises(kernels.KernelError,match='exact Marco decision provenance'): _use()
    _bind(tmp_path,monkeypatch,_registry())
    gate=kernels.fast_track_gate_registry()[GATE_ID]
    retained=' | '.join(gate['retains'])
    for phrase in ('exact task/branch/PR/head identity','independent semantic Review','Integration separation','production/destructive-operation safeguards','genuine platform/system impossibilities','wrong-SHA bundle rejection'):
        assert phrase in retained

def test_policy_has_no_project_overlay_or_fresh_chat_activation_dependency():
    root=(REPO_ROOT/'CLAUDE.md').read_text()
    doc=(DISH_ROOT/'docs'/'agents'/'fast-track-process.md').read_text()
    readme=(DISH_ROOT/'docs'/'chatgpt-projects'/'README.md').read_text()
    assert 'there is no ChatGPT Project-settings overlay' in root
    assert 'no ChatGPT Project-settings overlay' in doc
    assert 'fresh-chat activation step' in doc
    assert 'add no Project-settings payload' in readme
    assert 'MARCO OVERRIDE — FAST-TRACK PROCESS' not in root+doc

def test_wrong_nuisance_gate_is_fixed_at_source_and_generated_kernels_trigger_procedure():
    doc=(DISH_ROOT/'docs'/'agents'/'fast-track-process.md').read_text()
    assert 'Fix a wrong gate at source' in doc
    manifest,source=kernels.load_canonical()
    for role in source['roles']:
        rendered=kernels.render_role(manifest,source,role)
        assert 'Standing gate exception: use current reviewed Git only' in rendered
        assert 'dish/docs/agents/fast-track-process.md#Procedure' in rendered
