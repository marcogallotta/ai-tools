#!/usr/bin/env bash
set -euo pipefail
python3 <<'PY'
from pathlib import Path

GATE='repository-context-bundle-witness@1'
DIGEST='sha256:bbcf3768f1f0b0944a3c025cbd14f9c411787f029484bc4538ddac14a911a78c'

# The persisted Project overlay must carry the gate semantic identity, not only gate@version.
p=Path('dish/scripts/chatgpt_project_kernels.py')
s=p.read_text()
old="""def canonical_fast_track_overlay(value):
 if not isinstance(value,dict): raise KernelError('fast-track overlay must be an object')
 version=str(value.get('version','')).strip(); state=str(value.get('state','')).strip().upper(); generation=str(value.get('generation','')).strip(); scope=value.get('scope'); expiry=value.get('expiry'); reason=str(value.get('reason','')).strip()
 if version!=FAST_TRACK_OVERLAY_VERSION or state not in {'ACTIVE','INACTIVE'} or not generation or not isinstance(scope,list) or not scope or any(not isinstance(x,str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]*@[1-9][0-9]*',x.strip()) for x in scope) or (expiry is not None and not str(expiry).strip()): raise KernelError('invalid fast-track overlay fields')
 scope=sorted(set(x.strip() for x in scope))
 return {'version':version,'state':state,'generation':generation,'scope':scope,'expiry':None if expiry is None else str(expiry).strip(),'reason':reason}
"""
new="""def canonical_fast_track_overlay(value):
 if not isinstance(value,dict): raise KernelError('fast-track overlay must be an object')
 version=str(value.get('version','')).strip(); state=str(value.get('state','')).strip().upper(); generation=str(value.get('generation','')).strip(); scope=value.get('scope'); gate_semantics=value.get('gate_semantics'); expiry=value.get('expiry'); reason=str(value.get('reason','')).strip()
 if version!=FAST_TRACK_OVERLAY_VERSION or state not in {'ACTIVE','INACTIVE'} or not generation or not isinstance(scope,list) or not scope or any(not isinstance(x,str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]*@[1-9][0-9]*',x.strip()) for x in scope) or not isinstance(gate_semantics,dict) or (expiry is not None and not str(expiry).strip()): raise KernelError('invalid fast-track overlay fields')
 scope=sorted(set(x.strip() for x in scope))
 if set(gate_semantics)!=set(scope): raise KernelError('fast-track overlay gate semantics must exactly bind scope')
 normalized_semantics={}
 for key in scope:
  digest=str(gate_semantics[key]).strip().lower()
  if not re.fullmatch(r'sha256:[0-9a-f]{64}',digest): raise KernelError(f'fast-track overlay gate semantic digest invalid for {key}')
  normalized_semantics[key]=digest
 return {'version':version,'state':state,'generation':generation,'scope':scope,'gate_semantics':normalized_semantics,'expiry':None if expiry is None else str(expiry).strip(),'reason':reason}
"""
if s.count(old)!=1: raise SystemExit('canonical_fast_track_overlay baseline changed')
s=s.replace(old,new,1)
old=""" registry=fast_track_gate_registry(); gid=str(gate_id).strip(); gate=registry.get(gid)
 if not isinstance(gate_version,int) or gate is None or gate['current_version']!=gate_version: raise KernelError('fast-track gate is unknown, stale, or materially changed')
 if f'{gid}@{gate_version}' not in overlay['scope']: raise KernelError('fast-track gate is outside captured overlay scope')
 task=str(task).strip(); candidate=str(candidate).strip(); action=str(action).strip(); raw_evidence=str(raw_evidence).strip()
 if not task or not candidate or not action or not raw_evidence: raise KernelError('fast-track use requires exact task/candidate/action/raw evidence')
 return {'marker':'GATE WAIVED BY MARCO OVERRIDE','overlay_generation':overlay['generation'],'overlay_digest':fast_track_overlay_digest(overlay),'gate_id':gid,'gate_version':gate_version,'task':task,'candidate':candidate,'action':action,'raw_evidence':raw_evidence}
"""
new=""" registry=fast_track_gate_registry(); gid=str(gate_id).strip(); gate=registry.get(gid)
 if not isinstance(gate_version,int) or gate is None or gate['current_version']!=gate_version: raise KernelError('fast-track gate is unknown, stale, or materially changed')
 scope_key=f'{gid}@{gate_version}'
 if scope_key not in overlay['scope']: raise KernelError('fast-track gate is outside captured overlay scope')
 authorized_semantic_digest=overlay['gate_semantics'][scope_key]
 if authorized_semantic_digest!=gate['semantic_digest']: raise KernelError('fast-track gate is unknown, stale, or materially changed')
 task=str(task).strip(); candidate=str(candidate).strip(); action=str(action).strip(); raw_evidence=str(raw_evidence).strip()
 if not task or not candidate or not action or not raw_evidence: raise KernelError('fast-track use requires exact task/candidate/action/raw evidence')
 return {'marker':'GATE WAIVED BY MARCO OVERRIDE','overlay_generation':overlay['generation'],'overlay_digest':fast_track_overlay_digest(overlay),'gate_id':gid,'gate_version':gate_version,'gate_semantic_digest':authorized_semantic_digest,'task':task,'candidate':candidate,'action':action,'raw_evidence':raw_evidence}
"""
if s.count(old)!=1: raise SystemExit('fast_track_use baseline changed')
p.write_text(s.replace(old,new,1))

# Make the authoritative procedure explicit about the persisted semantic fence.
p=Path('dish/docs/agents/fast-track-process.md')
s=p.read_text()
s=s.replace('  "scope": ["repository-context-bundle-witness@1"],\n  "expiry": null,',f'  "scope": ["{GATE}"],\n  "gate_semantics": {{\n    "{GATE}": "{DIGEST}"\n  }},\n  "expiry": null,',1)
s=s.replace('captures the exact reserved block, generation, digest, scope and expiry presented at that bootstrap.','captures the exact reserved block, generation, digest, scope, gate-semantic digest bindings and expiry presented at that bootstrap.',1)
old2='2. **Resolve scope through current Git.** Every scope entry is an exact `<gate-id>@<version>` present as the current version in `dish/docs/chatgpt-projects/fast-track-gates.json`. Unknown gates, new gate classes and materially changed gate semantics are not inherited by an older overlay. A material gate change requires a new registry version and therefore an updated Project overlay or an ordinary exact Marco override. Wildcards are invalid.'
new2='2. **Resolve scope through current Git.** Every scope entry is an exact `<gate-id>@<version>` present as the current version in `dish/docs/chatgpt-projects/fast-track-gates.json`, and `gate_semantics` must persist the exact semantic digest authorized for that scope entry. Use requires the persisted digest to equal the current registry digest. Rewriting `waives`/`retains` for an existing gate version and recomputing the registry digest therefore makes an older overlay stale; it does not expand that overlay. Unknown gates, new gate classes and materially changed gate semantics are not inherited. A material gate change requires a new registry version plus an updated Project overlay scope/digest, or an ordinary exact Marco override. Wildcards are invalid.'
if s.count(old2)!=1: raise SystemExit('fast-track procedure scope baseline changed')
s=s.replace(old2,new2,1)
s=s.replace('Record `GATE WAIVED BY MARCO OVERRIDE` plus overlay generation/digest, exact gate ID/version, task, candidate, action and the raw failed evidence.','Record `GATE WAIVED BY MARCO OVERRIDE` plus overlay generation/digest, exact gate ID/version and gate semantic digest, task, candidate, action and the raw failed evidence.',1)
p.write_text(s)

# Keep the root bootstrap aligned without expanding the always-on kernel projection.
p=Path('CLAUDE.md')
s=p.read_text()
old='Apply only ACTIVE, unexpired exact gate ID/version scope entries that still match current [`dish/docs/chatgpt-projects/fast-track-gates.json`](dish/docs/chatgpt-projects/fast-track-gates.json); never inherit new/materially changed gates or wildcard future policy.'
new='Apply only ACTIVE, unexpired exact gate ID/version scope entries whose persisted gate-semantic digest exactly matches current [`dish/docs/chatgpt-projects/fast-track-gates.json`](dish/docs/chatgpt-projects/fast-track-gates.json); never inherit new/materially changed gates or wildcard future policy.'
if s.count(old)!=1: raise SystemExit('root fast-track scope baseline changed')
s=s.replace(old,new,1)
s=s.replace('Every use records `GATE WAIVED BY MARCO OVERRIDE` with overlay generation/digest + gate/version + exact task/candidate/action while preserving raw failed evidence.','Every use records `GATE WAIVED BY MARCO OVERRIDE` with overlay generation/digest + gate/version + gate semantic digest + exact task/candidate/action while preserving raw failed evidence.',1)
p.write_text(s)

# Strengthen focused regressions around same-version semantic rewrites.
p=Path('dish/tests/test_fast_track_overlay.py')
s=p.read_text()
s=s.replace("kernels=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(kernels)\n\ndef _overlay(**changes):\n    value={'version':'fasttrack-r3','state':'ACTIVE','generation':'g-2026-08-18','scope':['repository-context-bundle-witness@1'],'expiry':None,'reason':'avoid repeated repository-bundle transport waivers'}",f"kernels=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(kernels)\n\nGATE_DIGEST='{DIGEST}'\n\ndef _overlay(**changes):\n    value={{'version':'fasttrack-r3','state':'ACTIVE','generation':'g-2026-08-18','scope':['{GATE}'],'gate_semantics':{{'{GATE}':GATE_DIGEST}},'expiry':None,'reason':'avoid repeated repository-bundle transport waivers'}}",1)
s=s.replace("    assert result['gate_id']=='repository-context-bundle-witness' and result['gate_version']==1\n", "    assert result['gate_id']=='repository-context-bundle-witness' and result['gate_version']==1\n    assert result['gate_semantic_digest']==GATE_DIGEST\n",1)
s=s.replace("        _use(_overlay(scope=['repository-context-bundle-witness@2']),gate_version=2)","        _use(_overlay(scope=['repository-context-bundle-witness@2'],gate_semantics={'repository-context-bundle-witness@2':GATE_DIGEST}),gate_version=2)",1)
s=s.replace("        _use(_overlay(scope=['other-gate@1']))","        _use(_overlay(scope=['other-gate@1'],gate_semantics={'other-gate@1':GATE_DIGEST}))",1)
anchor="""def test_material_gate_change_requires_new_current_version(tmp_path,monkeypatch):
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
"""
addition=anchor+"""

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
"""
if s.count(anchor)!=1: raise SystemExit('material gate version regression baseline changed')
s=s.replace(anchor,addition,1)
s=s.replace("    assert set(('overlay_generation','overlay_digest','gate_id','gate_version','task','candidate','action','raw_evidence')) <= set(result)","    assert set(('overlay_generation','overlay_digest','gate_id','gate_version','gate_semantic_digest','task','candidate','action','raw_evidence')) <= set(result)",1)
p.write_text(s)
PY
