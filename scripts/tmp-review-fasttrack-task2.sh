#!/usr/bin/env bash
set -euo pipefail
: "${BASE_SHA:?BASE_SHA required}"
cp dish/docs/chatgpt-projects/source.json /tmp/task2-base-source.json
cp dish/docs/chatgpt-projects/manifest.json /tmp/task2-base-manifest.json
python3 <<'PY'
from pathlib import Path
import hashlib, json, re

task1_manifest=json.loads(Path('/tmp/task2-base-manifest.json').read_text())
task1_version=task1_manifest['canonical_version']

waiver=['exact-current repository-bundle retrieval/materialization/verification prerequisite when bundle transport is unavailable']
retains=[
    'live GitHub repository/current-main identity and source/history authority',
    'Asana task/orchestration authority',
    'exact task/branch/PR/head identity',
    'invalid/stale/mismatched/corrupt/wrong-SHA bundle rejection when any bundle is used',
    'independent semantic Review',
    'Integration separation',
    'production/destructive-operation safeguards',
    'genuine platform/system impossibilities'
]
semantic={'id':'repository-context-bundle-witness','version':1,'waives':waiver,'retains':retains}
digest='sha256:'+hashlib.sha256(json.dumps(semantic,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
registry={
    'schema_version':1,
    'overlay_version':'fasttrack-r3',
    'gates':[{
        'id':'repository-context-bundle-witness',
        'current_version':1,
        'versions':{'1':{'semantic_digest':digest,'waives':waiver,'retains':retains}}
    }]
}
Path('dish/docs/chatgpt-projects/fast-track-gates.json').write_text(json.dumps(registry,indent=2,sort_keys=True)+'\n')

Path('dish/docs/agents/fast-track-process.md').write_text('''# Fast-track Project overlay

The fast-track overlay is a narrow persistence surface for Marco's existing scoped process-override authority. It is not a second approval service, scheduler, queue, database, identity system, merge gate, or lifecycle authority.

## Reserved Project block

A ChatGPT Project may contain exactly one reserved block headed `MARCO OVERRIDE — FAST-TRACK PROCESS` followed by one JSON object:

```text
MARCO OVERRIDE — FAST-TRACK PROCESS
{
  "version": "fasttrack-r3",
  "state": "ACTIVE",
  "generation": "<operator-chosen generation>",
  "scope": ["repository-context-bundle-witness@1"],
  "expiry": null,
  "reason": "<optional operator reason>"
}
```

The overlay digest is `sha256:` plus SHA-256 of the canonical semantic JSON object (`sort_keys=True`, compact separators, UTF-8) after scope is de-duplicated and sorted. The digest identifies the captured operator input; it is not an anti-forgery signature.

## Procedure

1. **Capture only at a verified Project-settings boundary.** A verified new ChatGPT Project chat/session bootstrap captures the exact reserved block, generation, digest, scope and expiry presented at that bootstrap. Repository grounding has its own independent freshness identity. Ordinary in-session compaction or repository re-ground does **not** refresh, replace or silently re-read the captured Project overlay.
2. **Resolve scope through current Git.** Every scope entry is an exact `<gate-id>@<version>` present as the current version in `dish/docs/chatgpt-projects/fast-track-gates.json`. Unknown gates, new gate classes and materially changed gate semantics are not inherited by an older overlay. A material gate change requires a new registry version and therefore an updated Project overlay or an ordinary exact Marco override. Wildcards are invalid.
3. **Apply only an ACTIVE, unexpired captured generation.** When an in-scope gate blocks, preserve the raw failed/red evidence and continue only with the equivalent authoritative evidence or fallback allowed by that gate's registered semantics. Never relabel failed evidence as PASS.
4. **Record every use on an existing durable lifecycle surface.** Record `GATE WAIVED BY MARCO OVERRIDE` plus overlay generation/digest, exact gate ID/version, task, candidate, action and the raw failed evidence. Downstream Claude/Codex/Review/Integration consume that per-use record; they do not need Project-settings access.
5. **Current-chat Marco change/revocation is immediate.** `fast-track off`, an exact scope correction, or equivalent clear current-chat direction supersedes the captured generation immediately for that chat and is recorded durably when relevant. Project-settings edits are not presumed visible to an already-running chat.
6. **Expiry and new sessions are real boundaries.** An expired captured generation cannot be used. A later verified new Project chat/session captures the then-current Project settings instead of carrying the old generation forward. A future live Project-settings refresh primitive becomes an additional boundary only after its platform behavior is verified.

## Retained boundaries

The default registry does not waive exact task/branch/PR/head identity, independent semantic Review, Integration separation, production/destructive-operation safeguards, or genuine platform/system impossibilities. Adding such authority requires Marco to name that gate explicitly; it is never inherited from a broad phrase. Raw evidence remains truthful even when a gate is waived.

## Current initial gate

`repository-context-bundle-witness@1` waives only the exact-current repository-bundle retrieval/materialization/verification prerequisite when bundle transport is unavailable. GitHub/Asana authority, exact candidate identity, and invalid/stale/mismatched/corrupt/wrong-SHA bundle rejection remain required.
''')

root_path=Path('CLAUDE.md')
root=root_path.read_text()
anchor='Do not extend the waiver beyond the named scope; genuine platform/system constraints remain non-overridable.\n\n<!-- BEGIN GENERATED CHATTY WORK CONTRACT -->'
section='''Do not extend the waiver beyond the named scope; genuine platform/system constraints remain non-overridable.

### Persistent fast-track Project overlay

A reserved `MARCO OVERRIDE — FAST-TRACK PROCESS` block in ChatGPT Project settings is a persistence surface for the same scoped Marco override authority, interpreted only under [`dish/docs/agents/fast-track-process.md`](dish/docs/agents/fast-track-process.md). A verified new Project chat/session bootstrap captures its exact generation/digest separately from repository grounding; ordinary compaction/re-ground does not refresh Project settings. Current-chat Marco change/revocation is immediate. Apply only ACTIVE, unexpired exact gate ID/version scope entries that still match current [`dish/docs/chatgpt-projects/fast-track-gates.json`](dish/docs/chatgpt-projects/fast-track-gates.json); never inherit new/materially changed gates or wildcard future policy. Every use records `GATE WAIVED BY MARCO OVERRIDE` with overlay generation/digest + gate/version + exact task/candidate/action while preserving raw failed evidence. Exact identity, independent Review, Integration separation, destructive/production safeguards and genuine platform impossibilities remain outside the default fast-track scope.

<!-- BEGIN GENERATED CHATTY WORK CONTRACT -->'''
if root.count(anchor)!=1: raise SystemExit('root override anchor changed')
root_path.write_text(root.replace(anchor,section,1))

source_path=Path('dish/docs/chatgpt-projects/source.json')
source=json.loads(source_path.read_text())
shared=source.get('shared_rules')
if not isinstance(shared,list): raise SystemExit('shared_rules missing')
if any(x.get('id')=='fast-track-process-overlay' for x in shared if isinstance(x,dict)):
    raise SystemExit('fast-track-process-overlay already exists')
trigger='persistent fast-track Project override capture / use / revocation'
shared.append({
    'action_boundaries':['*'],
    'id':'fast-track-process-overlay',
    'impact':'additive',
    'surface':'authority',
    'text':'A reserved `MARCO OVERRIDE — FAST-TRACK PROCESS` Project block is session-captured operator authority independent of repository grounding. Capture its exact generation/digest only at a verified new Project chat/session bootstrap or a future proven refresh; ordinary re-ground never refreshes it, while explicit current-chat Marco change/revocation is immediate. Apply only ACTIVE/unexpired exact gate ID/version entries still current with unchanged semantics in `fast-track-gates.json`; never inherit unknown/new/materially changed gates or wildcards. Each use records `GATE WAIVED BY MARCO OVERRIDE` plus overlay generation/digest + gate/version + exact task/candidate/action while raw failure stays failed. Exact identity, independent Review, Integration separation, destructive/production safeguards and genuine platform impossibilities remain outside default scope.',
    'delivery':{'mode':'DIRECT_ALWAYS_ON'}
})
ctx=source.setdefault('context_dependencies',{})
triggers=ctx.setdefault('triggered_reads',{})
if trigger in triggers: raise SystemExit('fast-track triggered read already exists')
triggers[trigger]=['dish/docs/agents/fast-track-process.md#Procedure']
source_path.write_text(json.dumps(source,indent=2,sort_keys=True)+'\n')

script_path=Path('dish/scripts/chatgpt_project_kernels.py')
script=script_path.read_text()
old_import='import argparse, copy, hashlib, inspect, json, re, shlex, subprocess, sys\nfrom pathlib import Path'
new_import='import argparse, copy, hashlib, inspect, json, re, shlex, subprocess, sys\nfrom datetime import datetime, timezone\nfrom pathlib import Path'
if script.count(old_import)!=1: raise SystemExit('kernel import baseline changed')
script=script.replace(old_import,new_import,1)
old_paths="STANDING_INVARIANTS_PATH=DISH_ROOT/'docs'/'agents'/'standing-invariants.json'"
new_paths=old_paths+"\nFAST_TRACK_GATE_REGISTRY_PATH=PROJECT_DIR/'fast-track-gates.json'\nFAST_TRACK_OVERLAY_VERSION='fasttrack-r3'\nFAST_TRACK_OVERLAY_HEADER='MARCO OVERRIDE — FAST-TRACK PROCESS'"
if script.count(old_paths)!=1: raise SystemExit('kernel path baseline changed')
script=script.replace(old_paths,new_paths,1)
anchor="def _semantic_json_hash(v):return _h(json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())\ndef role_index_contracts()"
functions=r'''def _semantic_json_hash(v):return _h(json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())

def fast_track_gate_registry():
 raw=_read_json(FAST_TRACK_GATE_REGISTRY_PATH)
 if raw.get('schema_version')!=1 or raw.get('overlay_version')!=FAST_TRACK_OVERLAY_VERSION or not isinstance(raw.get('gates'),list): raise KernelError('fast-track gate registry schema/version mismatch')
 out={}
 for gate in raw['gates']:
  if not isinstance(gate,dict): raise KernelError('fast-track gate entries must be objects')
  gid=str(gate.get('id','')).strip(); current=gate.get('current_version'); versions=gate.get('versions')
  if not gid or gid in out or not re.fullmatch(r'[a-z0-9][a-z0-9-]*',gid) or not isinstance(current,int) or current<=0 or not isinstance(versions,dict): raise KernelError(f'invalid fast-track gate {gid!r}')
  entry=versions.get(str(current))
  if not isinstance(entry,dict) or not isinstance(entry.get('waives'),list) or not entry['waives'] or not isinstance(entry.get('retains'),list) or not entry['retains']: raise KernelError(f'fast-track gate {gid}@{current} current semantics missing')
  semantic={'id':gid,'version':current,'waives':entry['waives'],'retains':entry['retains']}
  digest='sha256:'+_semantic_json_hash(semantic)
  if entry.get('semantic_digest')!=digest: raise KernelError(f'fast-track gate {gid}@{current} semantic digest mismatch; material changes require a new gate version')
  out[gid]={'id':gid,'current_version':current,'semantic_digest':digest,'waives':list(entry['waives']),'retains':list(entry['retains'])}
 return out

def canonical_fast_track_overlay(value):
 if not isinstance(value,dict): raise KernelError('fast-track overlay must be an object')
 version=str(value.get('version','')).strip(); state=str(value.get('state','')).strip().upper(); generation=str(value.get('generation','')).strip(); scope=value.get('scope'); expiry=value.get('expiry'); reason=str(value.get('reason','')).strip()
 if version!=FAST_TRACK_OVERLAY_VERSION or state not in {'ACTIVE','INACTIVE'} or not generation or not isinstance(scope,list) or not scope or any(not isinstance(x,str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]*@[1-9][0-9]*',x.strip()) for x in scope) or (expiry is not None and not str(expiry).strip()): raise KernelError('invalid fast-track overlay fields')
 scope=sorted(set(x.strip() for x in scope))
 return {'version':version,'state':state,'generation':generation,'scope':scope,'expiry':None if expiry is None else str(expiry).strip(),'reason':reason}

def parse_fast_track_overlay_block(text):
 raw=str(text)
 if raw.count(FAST_TRACK_OVERLAY_HEADER)!=1: raise KernelError('Project settings must contain exactly one fast-track reserved header')
 tail=raw.split(FAST_TRACK_OVERLAY_HEADER,1)[1].lstrip()
 try:value,end=json.JSONDecoder().raw_decode(tail)
 except json.JSONDecodeError as e: raise KernelError(f'invalid fast-track overlay JSON: {e}') from e
 return canonical_fast_track_overlay(value)

def fast_track_overlay_digest(value): return 'sha256:'+_semantic_json_hash(canonical_fast_track_overlay(value))

def _fast_track_datetime(value,label):
 try: parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
 except ValueError as e: raise KernelError(f'invalid fast-track {label} datetime') from e
 if parsed.tzinfo is None: raise KernelError(f'fast-track {label} datetime must be timezone-aware')
 return parsed

def fast_track_use(value,*,gate_id,gate_version,task,candidate,action,raw_evidence,now=None):
 overlay=canonical_fast_track_overlay(value)
 if overlay['state']!='ACTIVE': raise KernelError('fast-track overlay is inactive')
 if overlay['expiry'] is not None:
  current=datetime.now(timezone.utc) if now is None else (now if isinstance(now,datetime) else _fast_track_datetime(now,'now'))
  if current.tzinfo is None: raise KernelError('fast-track now datetime must be timezone-aware')
  if _fast_track_datetime(overlay['expiry'],'expiry')<=current: raise KernelError('fast-track overlay generation is expired')
 registry=fast_track_gate_registry(); gid=str(gate_id).strip(); gate=registry.get(gid)
 if not isinstance(gate_version,int) or gate is None or gate['current_version']!=gate_version: raise KernelError('fast-track gate is unknown, stale, or materially changed')
 if f'{gid}@{gate_version}' not in overlay['scope']: raise KernelError('fast-track gate is outside captured overlay scope')
 task=str(task).strip(); candidate=str(candidate).strip(); action=str(action).strip(); raw_evidence=str(raw_evidence).strip()
 if not task or not candidate or not action or not raw_evidence: raise KernelError('fast-track use requires exact task/candidate/action/raw evidence')
 return {'marker':'GATE WAIVED BY MARCO OVERRIDE','overlay_generation':overlay['generation'],'overlay_digest':fast_track_overlay_digest(overlay),'gate_id':gid,'gate_version':gate_version,'task':task,'candidate':candidate,'action':action,'raw_evidence':raw_evidence}

def role_index_contracts()'''
if script.count(anchor)!=1: raise SystemExit('kernel semantic hash anchor changed')
script_path.write_text(script.replace(anchor,functions,1))

Path('dish/tests/test_fast_track_overlay.py').write_text('''from __future__ import annotations
import importlib.util, json
from datetime import datetime, timezone
from pathlib import Path
import pytest

DISH_ROOT=Path(__file__).resolve().parents[1]
REPO_ROOT=DISH_ROOT.parent
SCRIPT=DISH_ROOT/'scripts'/'chatgpt_project_kernels.py'
SPEC=importlib.util.spec_from_file_location('chatgpt_project_kernels_fast_track',SCRIPT); assert SPEC and SPEC.loader
kernels=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(kernels)

def _overlay(**changes):
    value={'version':'fasttrack-r3','state':'ACTIVE','generation':'g-2026-08-18','scope':['repository-context-bundle-witness@1'],'expiry':None,'reason':'avoid repeated repository-bundle transport waivers'}
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
    assert result['task']=='1217594495187308' and result['candidate']=='PR#169@abc'
    assert result['raw_evidence'].startswith('FAILED:')

def test_overlay_digest_is_semantic_and_stable_across_formatting_and_optional_reason():
    value=_overlay(scope=['repository-context-bundle-witness@1','repository-context-bundle-witness@1'],reason='')
    block='prefix text\\nMARCO OVERRIDE — FAST-TRACK PROCESS\\n'+json.dumps(value,indent=4,sort_keys=False)+'\\nremaining project text'
    parsed=kernels.parse_fast_track_overlay_block(block)
    assert parsed['scope']==['repository-context-bundle-witness@1'] and parsed['reason']==''
    assert kernels.fast_track_overlay_digest(parsed)==kernels.fast_track_overlay_digest(_overlay(reason=''))

def test_wildcard_unknown_and_future_gate_scope_are_not_inherited():
    with pytest.raises(kernels.KernelError,match='invalid fast-track overlay fields'):
        kernels.canonical_fast_track_overlay(_overlay(scope=['*@1']))
    with pytest.raises(kernels.KernelError,match='unknown, stale, or materially changed'):
        _use(_overlay(scope=['repository-context-bundle-witness@2']),gate_version=2)
    with pytest.raises(kernels.KernelError,match='outside captured overlay scope'):
        _use(_overlay(scope=['other-gate@1']))

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

def test_raw_failure_stays_failed_and_downstream_record_is_self_contained():
    result=_use()
    assert result['raw_evidence'].startswith('FAILED:')
    assert set(('overlay_generation','overlay_digest','gate_id','gate_version','task','candidate','action','raw_evidence')) <= set(result)

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
        assert 'MARCO OVERRIDE — FAST-TRACK PROCESS' in rendered
        assert 'ordinary re-ground never refreshes it' in rendered
        assert 'persistent fast-track Project override capture / use / revocation' in rendered
        assert 'dish/docs/agents/fast-track-process.md#Procedure' in rendered
''')

kernel_test=Path('dish/tests/test_chatgpt_project_kernels.py')
text=kernel_test.read_text()
fn='def test_required_version_inventory_matches_published_first_parent_history_and_restores_losses():'
start=text.index(fn)
end=text.find('\ndef ',start+len(fn))
if end<0: end=len(text)
block=text[start:end]
short=task1_version.rsplit('-',1)[-1]
list_match=re.search(r"(expected=\{f'dish-chatgpt-projects-v2-\{x\}' for x in \[)([^\]]*)(\]\})",block)
if not list_match: raise SystemExit('required-version fixture list not found')
items=list_match.group(2)
if f"'{short}'" not in items:
    items=items.rstrip()+",'"+short+"'"
block=block[:list_match.start(2)]+items+block[list_match.end(2):]
count_match=re.search(r'assert set\(versions\)==expected and len\(versions\)==(\d+)',block)
if not count_match: raise SystemExit('required-version fixture count not found')
expected_count=len(re.findall(r"'[0-9a-f]{12}'",items))+1
block=block[:count_match.start(1)]+str(expected_count)+block[count_match.end(1):]
kernel_test.write_text(text[:start]+block+text[end:])
PY

python3 dish/scripts/chatgpt_project_kernels.py reconcile \
  --base-manifest /tmp/task2-base-manifest.json \
  --base-source /tmp/task2-base-source.json \
  --source dish/docs/chatgpt-projects/source.json \
  --output dish/docs/chatgpt-projects/manifest.json
python3 dish/scripts/chatgpt_project_kernels.py render
python3 dish/scripts/chatgpt_project_kernels.py check
(cd dish && .venv/bin/python -m pytest -q tests/test_chatgpt_project_kernels.py tests/test_review_bundle_consistency.py tests/test_fast_track_overlay.py)
git diff --check

git add CLAUDE.md dish/docs/agents/fast-track-process.md dish/docs/chatgpt-projects dish/scripts/chatgpt_project_kernels.py dish/tests/test_chatgpt_project_kernels.py dish/tests/test_fast_track_overlay.py
git commit -m $'Implement FastTrack-R3 Project override overlay\n\nAsana task: 1217587510908418'
echo "head=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"
python3 - <<'PY' >> "$GITHUB_OUTPUT"
import json
print('version='+json.load(open('dish/docs/chatgpt-projects/manifest.json'))['canonical_version'])
PY
