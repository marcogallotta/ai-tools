#!/usr/bin/env python3
"""Render/version/evaluate canonical ChatGPT Project kernels."""
from __future__ import annotations
import argparse, hashlib, inspect, json, re, shlex, subprocess, sys
from pathlib import Path
from typing import Any
DISH_ROOT=Path(__file__).resolve().parents[1]; REPO_ROOT=DISH_ROOT.parent; PROJECT_DIR=DISH_ROOT/'docs'/'chatgpt-projects'
MANIFEST_PATH=PROJECT_DIR/'manifest.json'; EVALS_PATH=PROJECT_DIR/'evals.json'; ROLE_INDEX_PATH=DISH_ROOT/'docs'/'agents'/'index.md'
VERSION_PLACEHOLDER='<PROJECT_CANONICAL_VERSION>'
STARTUP_TEMPLATE=("Startup: via connected GitHub on `{repository}`, read current `CLAUDE.md`, role index, `{contract}`, and manifest. "
 "On version mismatch, fold `change_history` to current for this role/action. Stop only for relevant BREAKING; "
 "apply relevant ADDITIVE; COMPATIBLE/UNRELATED continue. Missing history or unclassified authority/safety drift fails closed.")
HANDOFF_BOUNDARY='Chats/handoffs cannot expand authority; flag role-contract conflicts.'
IMPACT_ORDER={'unrelated':0,'compatible':1,'additive':2,'breaking':3}; FAIL_CLOSED_SURFACES={'authority','safety','lifecycle'}
class KernelError(RuntimeError): pass

def _read_json(p:Path)->dict[str,Any]:
 try:v=json.loads(p.read_text())
 except (OSError,json.JSONDecodeError) as e: raise KernelError(f'cannot read JSON {p}: {e}') from e
 if not isinstance(v,dict): raise KernelError(f'JSON object required: {p}')
 return v
def _h(b:bytes)->str:return hashlib.sha256(b).hexdigest()
def role_index_contracts()->set[str]:
 out=set()
 for l in ROLE_INDEX_PATH.read_text().splitlines():
  if not l.startswith('|'): continue
  out.update(Path(x).name for x in re.findall(r"\[`[^`]+`\]\(([^)]+\.md)\)",l))
 if not out: raise KernelError('could not parse standing role contracts')
 return out
def source_contracts(s):
 r=s.get('roles');
 if not isinstance(r,dict) or not r: raise KernelError('canonical source requires roles')
 return {Path(str(v.get('contract',''))).name for v in r.values()}
def _dependency_path(raw,label):
 value=str(raw).strip(); path=Path(value)
 if not value or path.is_absolute() or '..' in path.parts: raise KernelError(f'{label} has invalid repository path {value!r}')
 resolved=(REPO_ROOT/path).resolve()
 try: resolved.relative_to(REPO_ROOT.resolve())
 except ValueError as e: raise KernelError(f'{label} escapes repository: {value!r}') from e
 if not resolved.is_file(): raise KernelError(f'{label} dependency does not exist: {value!r}')
 return value
def context_dependencies(s,role):
 raw=s['roles'][role].get('context_dependencies')
 if raw is None:return None
 if not isinstance(raw,dict): raise KernelError(f'roles.{role}.context_dependencies must be an object')
 preload=raw.get('preload'); action=raw.get('action_specific')
 if not isinstance(preload,dict) or preload.get('role_index_contracts') is not True: raise KernelError(f'roles.{role}.context_dependencies.preload must require role_index_contracts')
 additional=preload.get('additional')
 if not isinstance(additional,list) or not additional: raise KernelError(f'roles.{role}.context_dependencies.preload.additional must be a non-empty list')
 additional=[_dependency_path(x,f'roles.{role}.context_dependencies.preload.additional') for x in additional]
 if not isinstance(action,dict) or not action: raise KernelError(f'roles.{role}.context_dependencies.action_specific must be a non-empty object')
 normalized={}
 for boundary,paths in action.items():
  key=str(boundary).strip()
  if not key or not isinstance(paths,list) or not paths: raise KernelError(f'roles.{role}.context_dependencies.action_specific entries require a label and paths')
  normalized[key]=[_dependency_path(x,f'roles.{role}.context_dependencies.action_specific.{key}') for x in paths]
 return {'preload':{'role_index_contracts':True,'additional':additional},'action_specific':normalized}
def validate_topology(s):
 a,b=role_index_contracts(),source_contracts(s)
 if a!=b: raise KernelError(f'Project topology differs from role index: index={sorted(a)} source={sorted(b)}')
 for role in s['roles']: context_dependencies(s,role)
def _rules(v,label):
 if not isinstance(v,list): raise KernelError(f'{label} must be a list')
 out=[]; seen=set()
 for x in v:
  if not isinstance(x,dict): raise KernelError(f'{label} entries must be objects')
  rid=str(x.get('id','')).strip(); text=str(x.get('text','')).strip(); impact=str(x.get('impact','')).strip(); surface=str(x.get('surface','')).strip(); bounds=x.get('action_boundaries')
  if not rid or not text: raise KernelError(f'{label} rule requires id and text')
  if impact not in {'breaking','additive','compatible'}: raise KernelError(f'{label} rule {rid} requires impact')
  if not surface or not isinstance(bounds,list) or not bounds or any(not str(z).strip() for z in bounds): raise KernelError(f'{label} rule {rid} requires surface/action_boundaries')
  if rid in seen: raise KernelError(f'duplicate rule id {rid}')
  seen.add(rid); out.append({'id':rid,'text':text,'impact':impact,'surface':surface,'action_boundaries':[str(z).strip() for z in bounds]})
 return out
def repository_config(s):
 repo=str(s.get('repository_full_name','')).strip(); branch=str(s.get('default_branch','')).strip(); transport=str(s.get('github_transport','')).strip()
 if not repo or repo.count('/')!=1: raise KernelError('canonical source requires repository_full_name in owner/name form')
 if not branch: raise KernelError('canonical source requires default_branch')
 if not transport: raise KernelError('canonical source requires github_transport')
 return repo,branch,transport
def effective_rules(s,role):
 rs=_rules(s.get('shared_rules'),'shared_rules')+_rules(s['roles'][role].get('rules'),f'roles.{role}.rules'); ids=[x['id'] for x in rs]
 if len(ids)!=len(set(ids)): raise KernelError(f'duplicate effective rules for {role}')
 return rs
def _render_context_dependencies(s,role):
 deps=context_dependencies(s,role)
 if deps is None:return []
 extra=' + '.join(f'`{x}`' for x in deps['preload']['additional'])
 actions=[]
 for label,paths in deps['action_specific'].items(): actions.append(f"{label} -> {' + '.join(f'`{x}`' for x in paths)}")
 return [f'Read-only decision context (startup/re-grounding): load every standing role contract listed by the current role index + {extra} before lifecycle/test/Integration-mechanics conclusions. Reading them grants no Implementation, Review, Integration, merge, or production authority; only an explicit allowed composition below can expand authority.',f"Action-specific context refresh: {'; '.join(actions)}."]
def render_role_with_version(s,role,version):
 r=s['roles'][role]; comps=r.get('allowed_compositions',[]); repo,branch,_=repository_config(s)
 if not isinstance(comps,list): raise KernelError(f'roles.{role}.allowed_compositions must be a list')
 lines=[f"# {r['project_name']}",'',f"PROJECT_ROLE: {r['default_role']}",f'PROJECT_CANONICAL_VERSION: {version}','CANONICAL_MANIFEST: dish/docs/chatgpt-projects/manifest.json',f"ROLE_CONTRACT: {r['contract']}",f'PROJECT_REPOSITORY: {repo}',f'PROJECT_DEFAULT_BRANCH: {branch}','',STARTUP_TEMPLATE.format(repository=repo,contract=r['contract'])]
 lines += _render_context_dependencies(s,role)+['',f"Role: **{r['default_role']}**."]
 if comps: lines+=['Allowed composition only when explicitly triggered by current authority:']+[f'- {x}' for x in comps]
 else: lines+=['No implicit role composition is permitted.']
 lines += [HANDOFF_BOUNDARY,'','High-consequence rules:']+[f"- {x['text']}" for x in effective_rules(s,role)]+['']
 return '\n'.join(lines)
def kernel_identity(s):
 repository_config(s); b=bytearray()
 for role in sorted(s['roles']):
  b+=role.encode()+b'\0'+render_role_with_version(s,role,VERSION_PLACEHOLDER).encode()+b'\0'
  md=[{k:x[k] for k in ('id','impact','surface','action_boundaries')} for x in effective_rules(s,role)]
  b+=json.dumps(md,sort_keys=True,separators=(',',':')).encode()+b'\0'
 return _h(bytes(b))
def _rule_fingerprint(x):return _h(json.dumps({k:x.get(k) for k in ('id','text','impact','surface','action_boundaries')},sort_keys=True,separators=(',',':')).encode())
def rule_fingerprints(s):return {r:{x['id']:_rule_fingerprint(x) for x in effective_rules(s,r)} for r in s['roles']}
def renderer_fingerprint():
 return _h('\0'.join((STARTUP_TEMPLATE,HANDOFF_BOUNDARY,inspect.getsource(repository_config),inspect.getsource(context_dependencies),inspect.getsource(_render_context_dependencies),inspect.getsource(render_role_with_version),inspect.getsource(kernel_identity))).encode())
def _impact(c):
 x=str(c.get('impact','')).strip(); surface=str(c.get('surface','')).strip()
 if x in IMPACT_ORDER:return x
 if surface in FAIL_CLOSED_SURFACES:return 'breaking'
 return 'compatible'
def _incoming(manifest):
 c=str(manifest['canonical_version']); e=[x for x in manifest['change_history'] if str(x.get('to_version'))==c]
 if len(e)!=1: raise KernelError('canonical version must have exactly one incoming change_history edge')
 return e[0]
def _validate_current_edge_classification(m,s):
 e=_incoming(m); prior=e.get('from_rule_fingerprints'); roles=set(s['roles'])
 if not isinstance(prior,dict) or not isinstance(prior.get('_shared'),dict) or not isinstance(prior.get('_roles'),dict) or set(prior['_roles'])!=roles: raise KernelError('current edge requires _shared/_roles prior fingerprints')
 cur=rule_fingerprints(s); changed=set()
 for role in roles:
  old={**prior['_shared'],**prior['_roles'][role]}
  for rid in set(old)|set(cur[role]):
   if old.get(rid)!=cur[role].get(rid): changed.add((rid,role))
 classified=set(); renderer=[]
 for c in e['changes']:
  rid=str(c['rule_id']); rs=set(c['roles'])
  if rid.startswith('renderer:'): renderer.append(c); continue
  for role in (roles if '*' in rs else rs): classified.add((rid,role))
 miss=sorted(changed-classified); extra=sorted(classified-changed)
 if miss or extra: raise KernelError(f'current drift edge classification mismatch: missing={miss} extras={extra}')
 oldr=str(e.get('from_renderer_fingerprint','')).strip(); changedr=oldr!=renderer_fingerprint()
 if not oldr: raise KernelError('current edge requires from_renderer_fingerprint')
 if changedr and not renderer: raise KernelError('renderer changed without renderer:* classification')
 if not changedr and renderer: raise KernelError('renderer classifications exist but renderer unchanged')
def validate_change_history(m,s):
 h=m.get('change_history'); roles=set(s['roles']); ids={x['id'] for r in roles for x in effective_rules(s,r)}
 if not isinstance(h,list) or not h: raise KernelError('manifest.change_history must be non-empty')
 for edge in h:
  prior=edge.get('from_rule_fingerprints') if isinstance(edge,dict) else None
  if not isinstance(prior,dict): continue
  shared=prior.get('_shared')
  if isinstance(shared,dict): ids.update(map(str,shared))
  role_maps=prior.get('_roles')
  if isinstance(role_maps,dict):
   for fingerprints in role_maps.values():
    if isinstance(fingerprints,dict): ids.update(map(str,fingerprints))
 seen=set()
 for e in h:
  a=str(e.get('from_version','')).strip(); b=str(e.get('to_version','')).strip(); ch=e.get('changes')
  if not a or not b or a==b or a in seen: raise KernelError(f'ambiguous/invalid change_history transition from {a!r}')
  seen.add(a)
  if not isinstance(ch,list) or not ch: raise KernelError(f'change_history {a}->{b} requires changes')
  for c in ch:
   rid=str(c.get('rule_id','')).strip(); rs=c.get('roles'); bounds=c.get('action_boundaries'); surf=str(c.get('surface','')).strip()
   if not rid or (rid not in ids and not rid.startswith('renderer:')): raise KernelError(f'change_history references unknown rule {rid!r}')
   if not isinstance(rs,list) or not rs or (set(rs)!={'*'} and not set(rs).issubset(roles)): raise KernelError(f'change_history {rid} has invalid roles')
   if not isinstance(bounds,list) or not bounds or not surf: raise KernelError(f'change_history {rid} requires boundaries/surface')
   _impact(c)
 if not any(str(e.get('to_version'))==str(m.get('canonical_version')) for e in h): raise KernelError('change_history lacks canonical transition')
 _validate_current_edge_classification(m,s)
def load_canonical():
 m=_read_json(MANIFEST_PATH); p=PROJECT_DIR/str(m.get('source_file','')); s=_read_json(p)
 if m.get('source_sha256')!=_h(json.dumps(s,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()): raise KernelError('canonical source semantic hash mismatch')
 if s.get('schema_version')!=m.get('schema_version'): raise KernelError('manifest/source schema mismatch')
 kid=kernel_identity(s)
 if m.get('kernel_identity_sha256')!=kid: raise KernelError('rendered kernel identity mismatch')
 exp=f"{m.get('version_namespace','')}-{kid[:12]}"
 if m.get('canonical_version')!=exp: raise KernelError(f'canonical_version must be {exp!r}')
 validate_change_history(m,s); return m,s
def render_role(m,s,role):return render_role_with_version(s,role,str(m['canonical_version']))
def generated_paths(m,s):
 files=m.get('generated_role_files')
 if not isinstance(files,dict) or set(files)!=set(s['roles']): raise KernelError('generated role file map mismatch')
 return {r:PROJECT_DIR/str(files[r]) for r in s['roles']}
def render_all(*,check):
 m,s=load_canonical(); limit=int(m.get('max_kernel_chars',3500)); out=[]
 for r,p in generated_paths(m,s).items():
  text=render_role(m,s,r); n=len(text)
  if n>limit: raise KernelError(f'kernel {r} exceeds {limit} chars: {n}')
  if check:
   if not p.is_file() or p.read_text()!=text: raise KernelError(f'generated kernel differs: {p}')
  else:p.write_text(text)
  out.append((r,n))
 return out
def _change_path(m,v):
 if v==m['canonical_version']: return []
 by={str(e['from_version']):e for e in m['change_history']}; out=[]; seen=set(); cur=v
 while cur!=m['canonical_version']:
  if cur in seen or cur not in by: raise KernelError(f'change_history does not cover {v} to {m["canonical_version"]}')
  seen.add(cur); e=by[cur]; out.append(e); cur=str(e['to_version'])
 return out
def classify_project_drift(project_version,role_key,action_boundary,*,manifest=None,source=None):
 if manifest is None or source is None:
  mm,ss=load_canonical(); manifest=manifest or mm; source=source or ss
 if role_key not in source['roles']: raise KernelError(f'unknown Project role: {role_key}')
 boundary=str(action_boundary).strip()
 if not boundary: raise KernelError('drift classification requires action_boundary')
 canonical=str(manifest['canonical_version'])
 if project_version==canonical:return {'project_version':project_version,'canonical_version':canonical,'role':role_key,'action_boundary':boundary,'impact':'unrelated','block':False,'resync_required':False,'changes':[]}
 applicable=[]; effective=[]; blocking=[]
 for e in _change_path(manifest,project_version):
  for raw in e['changes']:
   if '*' not in raw['roles'] and role_key not in raw['roles']: continue
   c=dict(raw,from_version=e['from_version'],to_version=e['to_version']); c['impact']=_impact(c); applicable.append(c)
   rel='*' in c['action_boundaries'] or boundary in c['action_boundaries']
   if c['impact']=='breaking' and rel: effective.append(c); blocking.append(c)
   elif c['impact'] in {'additive','compatible'}: effective.append(c)
 impact='breaking' if blocking else (max((x['impact'] for x in effective),key=IMPACT_ORDER.get) if effective else 'unrelated')
 return {'project_version':project_version,'canonical_version':canonical,'role':role_key,'action_boundary':boundary,'impact':impact,'block':bool(blocking),'resync_required':bool(applicable),'changes':effective}

REQUIRED_EVAL_IDS={'active-gate-blocker-cannot-be-deferred','additive-evidence-drift','allowed-specialist-implementation-composition','audit-dedupe-existing-finding','audit-exact-baseline','audit-missing-authority-fails-closed','audit-moved-baseline-current-blocker','audit-new-finding-backlog-only','audit-refuses-mutation-authority','audit-specialist-context-no-authority','authenticated-account-not-human-decision','chat-only-review-verdict-not-complete','code-smell-dedupe-log-and-continue','code-smell-true-blocker-stays-active','comparison-incompatible-target-escalates-implementation','compatible-concise-output-drift','compatible-wording-drift','configured-repository-pr-routing','coordinator-check-everything-mixed-state','coordinator-pr-intake-automatic-review','cross-role-context-bleed','current-template-lookup','development-workflow-context-preload-no-authority','development-workflow-pr40-fallback-context','development-workflow-pr60-test-scope-context','disposable-fixture-still-needs-health','durable-review-classification','forbidden-implicit-role-expansion','friction-active-blocker-routes-to-active-work','friction-dedupe-no-urgency','handoff-conflicts-with-role-authority','implementation-escalation-is-action-first','implementation-rejects-patch-only-completion','integration-breaking-merge-drift','integration-rejects-head-mismatch','live-authority-over-stale-memory','no-valid-fallback','publication-blocker-forbids-unsafe-shortcuts','publication-completion-invalidates-prior-review','publication-fully-published-local-certification','publication-handoff-before-human-notification','publication-unsafe-governed-path-blocker','repository-friction-discovery','review-breaking-completion-drift','review-exact-head-completion','reviewed-head-movement-classification','separate-pr-does-not-clear-independent-blocker','shared-resource-concurrency-preflight','skipped-version-breaking-drift','skipped-version-nonbreaking-drift','stale-project-version','supported-operation-stays-local-system-access','task-history-before-no-op','unrelated-role-drift','valid-action-fallback'}
ORACLE_FIELDS={'expected','failure','expected_outcome','required_actions','forbidden_actions','required_observations','required_observations_by_role','require_ordered_observations','observation_link_field'}
def _eval_payload():return _read_json(EVALS_PATH)
def _evals():
 x=_eval_payload().get('scenarios');
 if not isinstance(x,list): raise KernelError('evals scenarios must be a list')
 return x
def _obs_pattern(p,sid):
 if not isinstance(p,dict) or not str(p.get('kind','')).strip() or not str(p.get('operation','')).strip(): raise KernelError(f'eval {sid} invalid observation pattern')
 if 'equals' in p and not isinstance(p['equals'],dict): raise KernelError(f'eval {sid} observation equals must be object')
def validate_eval_contracts():
 m,s=load_canonical(); payload=_eval_payload(); default_expected=str(payload.get('default_expected','')).strip(); default_failure=str(payload.get('default_failure','')).strip(); seen=set(); out=[]
 for q in _evals():
  sid=str(q.get('id','')).strip(); roles=q.get('roles'); rr=q.get('required_rules'); ra=q.get('required_actions'); fa=q.get('forbidden_actions')
  if not sid or sid in seen or not isinstance(roles,list) or not roles or not set(roles).issubset(s['roles']): raise KernelError(f'invalid eval {sid!r}')
  seen.add(sid); out.append(sid)
  if not isinstance(rr,list) or not isinstance(ra,list) or not ra or not isinstance(fa,list) or not str(q.get('expected_outcome','')).strip(): raise KernelError(f'eval {sid} missing contract fields')
  if not str(q.get('expected',default_expected)).strip() or not str(q.get('failure',default_failure)).strip(): raise KernelError(f'eval {sid} missing expected/failure')
  for role in roles:
   missing=set(rr)-{x['id'] for x in effective_rules(s,role)}
   if missing: raise KernelError(f'eval {sid} lacks rules for {role}: {sorted(missing)}')
  prompts=q.get('prompts')
  if prompts is None:
   if not str(q.get('prompt','')).strip(): raise KernelError(f'eval {sid} requires prompt')
  elif not isinstance(prompts,dict) or set(prompts)!=set(roles) or any(not str(prompts[r]).strip() for r in roles): raise KernelError(f'eval {sid} prompts mismatch roles')
  common=q.get('required_observations',[]); by=q.get('required_observations_by_role',{})
  if not isinstance(common,list) or not isinstance(by,dict) or not set(by).issubset(roles): raise KernelError(f'eval {sid} invalid observations')
  for pats in [common,*by.values()]:
   if not isinstance(pats,list): raise KernelError(f'eval {sid} observations must be lists')
   for p in pats:_obs_pattern(p,sid)
  for role in roles:
   pats=by.get(role,common)
   if q.get('require_ordered_observations') and not pats: raise KernelError(f'eval {sid} cannot order absent observations')
   if q.get('observation_link_field') and len(pats)<2: raise KernelError(f'eval {sid} link field needs multiple observations')
 if seen!=REQUIRED_EVAL_IDS: raise KernelError(f'eval set mismatch missing={sorted(REQUIRED_EVAL_IDS-seen)} extras={sorted(seen-REQUIRED_EVAL_IDS)}')
 return out
def _actions():return sorted({str(x) for q in _evals() for k in ('required_actions','forbidden_actions') for x in q.get(k,[])})
def prepare_eval_bundle():
 m,s=load_canonical(); validate_eval_contracts(); cases=[]
 for q in _evals():
  for role in q['roles']:
   text=render_role(m,s,role); case={'case_id':f"{q['id']}::{role}",'scenario_id':q['id'],'role':role,'project_name':s['roles'][role]['project_name'],'kernel_sha256':_h(text.encode()),'project_instructions':text,'prompt':str(q.get('prompts',{}).get(role,q.get('prompt','')))}
   if ORACLE_FIELDS & set(case): raise KernelError('prepared eval exposes oracle')
   cases.append(case)
 return {'schema_version':2,'runner_protocol':'dish-chatgpt-project-behavior-v2','canonical_version':m['canonical_version'],'fresh_chat_requirement':'Use a newly created chat for every case.','response_contract':{'instruction':'Return assistant_response plus independent runner-observed tool evidence.','action_vocabulary':_actions(),'runner_observation_shape':{'seq':'<positive integer>','kind':'<event kind>','operation':'<tool operation>'}},'cases':cases}
def _oracles():
 out={}
 for q in _evals():
  for role in q['roles']:
   out[f"{q['id']}::{role}"]={'expected_outcome':str(q['expected_outcome']),'required_actions':set(map(str,q['required_actions'])),'forbidden_actions':set(map(str,q['forbidden_actions'])),'required_observations':list(q.get('required_observations_by_role',{}).get(role,q.get('required_observations',[]))),'require_ordered_observations':bool(q.get('require_ordered_observations')),'observation_link_field':str(q.get('observation_link_field','')).strip()}
 return out
def _contains(a,b):
 if isinstance(a,dict) and isinstance(b,dict): return all(k in a and _contains(a[k],v) for k,v in b.items())
 if isinstance(a,list): return b in a if not isinstance(b,list) else all(x in a for x in b)
 return a==b
def _match(o,p):return o.get('kind')==p.get('kind') and o.get('operation')==p.get('operation') and _contains(o,p.get('equals',{})) and _contains(o,p.get('contains',{}))
def _validate_observed_evidence(cid,o,obs):
 pats=o['required_observations']; obs=[] if obs is None else obs
 if not isinstance(obs,list): raise KernelError(f'behavior result {cid} runner_observations must be a list')
 if pats and not obs: raise KernelError(f'behavior eval failed for {cid}: missing runner-observed evidence')
 norm=[]; seq=[]
 for x in obs:
  if not isinstance(x,dict) or not isinstance(x.get('seq'),int) or x['seq']<=0: raise KernelError(f'behavior result {cid} invalid observation')
  norm.append(dict(x)); seq.append(x['seq'])
 if len(set(seq))!=len(seq): raise KernelError(f'behavior result {cid} duplicate observation seq')
 norm.sort(key=lambda x:x['seq'])
 for x in norm:
  if str(x.get('operation','')) in o['forbidden_actions']: raise KernelError(f"behavior eval failed for {cid}: runner observed forbidden operation {x.get('operation')!r}")
 found=[]; start=0
 for p in pats:
  idx=next((i for i in range(start if o['require_ordered_observations'] else 0,len(norm)) if _match(norm[i],p)),None)
  if idx is None: raise KernelError(f'behavior eval failed for {cid}: missing required runner observation {p!r}')
  found.append(norm[idx]); start=idx+1
 link=o['observation_link_field']
 if link:
  vals=[x.get(link) for x in found if x.get(link) not in (None,'')]
  if len(vals)<2 or len(set(map(str,vals)))!=1: raise KernelError(f'behavior eval failed for {cid}: required observations do not share {link}')
def evaluate_behavior_results(p):
 m,_=load_canonical(); validate_eval_contracts()
 if p.get('schema_version')!=2 or p.get('runner_protocol')!='dish-chatgpt-project-behavior-v2' or p.get('canonical_version')!=m['canonical_version']: raise KernelError('behavior results metadata mismatch')
 results=p.get('results'); oracles=_oracles(); vocab=set(_actions()); by={}; chats=set()
 if not isinstance(results,list): raise KernelError('behavior results require results list')
 for r in results:
  cid=str(r.get('case_id','')); chat=str(r.get('fresh_chat_id','')); resp=r.get('assistant_response')
  if cid not in oracles or cid in by: raise KernelError(f'unknown or duplicate behavior result case: {cid!r}')
  if not chat or chat in chats: raise KernelError(f'missing or reused fresh_chat_id for {cid}: {chat!r}')
  if not isinstance(resp,dict): raise KernelError(f'behavior result {cid} assistant_response must be an object')
  chats.add(chat); acts=resp.get('actions')
  if not isinstance(acts,list) or not acts: raise KernelError(f'behavior result {cid} requires actions')
  acts=set(map(str,acts)); unknown=acts-vocab; o=oracles[cid]
  if unknown or str(resp.get('outcome',''))!=o['expected_outcome'] or o['required_actions']-acts or o['forbidden_actions']&acts: raise KernelError(f'behavior eval failed for {cid}')
  _validate_observed_evidence(cid,o,r.get('runner_observations')); by[cid]=r
 missing=set(oracles)-set(by)
 if missing: raise KernelError(f'behavior results missing cases: {sorted(missing)}')
 return sorted(by)
def run_fresh_chat_runner(command):
 argv=shlex.split(command)
 if not argv: raise KernelError('runner command empty')
 b=prepare_eval_bundle(); results=[]
 for case in b['cases']:
  inp=dict(case,fresh_chat_requirement=b['fresh_chat_requirement'],response_contract=b['response_contract']); c=subprocess.run(argv,input=json.dumps(inp),text=True,capture_output=True)
  if c.returncode: raise KernelError(f'fresh-chat runner failed for {case["case_id"]}: {c.stderr.strip()}')
  try:r=json.loads(c.stdout)
  except json.JSONDecodeError as e: raise KernelError(f'invalid runner JSON for {case["case_id"]}') from e
  results.append({'case_id':case['case_id'],'fresh_chat_id':r.get('fresh_chat_id'),'assistant_response':r.get('assistant_response'),'runner_observations':r.get('runner_observations',[])})
 return {'schema_version':2,'runner_protocol':b['runner_protocol'],'canonical_version':b['canonical_version'],'results':results}
def version_status(project_version,role_key,action_boundary):
 m,s=load_canonical(); c=str(m['canonical_version'])
 if project_version==c:return True,f'PROJECT INSTRUCTIONS CURRENT — {c}'
 d=classify_project_drift(project_version,role_key,action_boundary,manifest=m,source=s)
 if d['block']:return False,f'PROJECT INSTRUCTIONS BREAKING DRIFT — project={project_version} repository={c} role={role_key} boundary={action_boundary}; resynchronize the affected Project before this action'
 return True,f"PROJECT INSTRUCTIONS {str(d['impact']).upper()} DRIFT — project={project_version} repository={c} role={role_key} boundary={action_boundary}; continue under current repository authority"
def command_check():
 m,s=load_canonical(); validate_topology(s); rr=render_all(check=True); ee=validate_eval_contracts(); print(f"canonical_version={m['canonical_version']}"); print(f"kernel_identity_sha256={m['kernel_identity_sha256']}")
 for r,n in rr: print(f'PASS kernel {r}: {n} chars')
 for x in ee: print(f'PASS eval-contract {x}')
def _write_json(p,v):p.write_text(json.dumps(v,indent=2,sort_keys=True)+'\n')
def _parser():
 p=argparse.ArgumentParser(description=__doc__); s=p.add_subparsers(dest='command',required=True); r=s.add_parser('render'); r.add_argument('--check',action='store_true'); s.add_parser('check'); pe=s.add_parser('prepare-eval'); pe.add_argument('--output',required=True,type=Path); ev=s.add_parser('eval'); g=ev.add_mutually_exclusive_group(required=True); g.add_argument('--results',type=Path); g.add_argument('--runner-command'); ev.add_argument('--save-results',type=Path); v=s.add_parser('version'); v.add_argument('--project-version',required=True); v.add_argument('--role',required=True); v.add_argument('--action-boundary',default='role-critical-write'); return p
def main():
 a=_parser().parse_args()
 try:
  if a.command=='render':
   for r,n in render_all(check=a.check): print(f'PASS {r}: {n} chars')
  elif a.command=='check': command_check()
  elif a.command=='prepare-eval': _write_json(a.output,prepare_eval_bundle()); print(f'WROTE {a.output}')
  elif a.command=='eval':
   p=_read_json(a.results) if a.results else run_fresh_chat_runner(a.runner_command)
   if a.save_results:
    if a.results: raise KernelError('--save-results only with --runner-command')
    _write_json(a.save_results,p)
   for x in evaluate_behavior_results(p): print(f'PASS behavior {x}')
  else:
   ok,msg=version_status(a.project_version,a.role,a.action_boundary); print(msg); return 0 if ok else 3
 except KernelError as e: print(f'ERROR: {e}',file=sys.stderr); return 2
 return 0
if __name__=='__main__': raise SystemExit(main())
