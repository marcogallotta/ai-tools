#!/usr/bin/env python3
"""Render/version/evaluate canonical ChatGPT Project kernels."""
from __future__ import annotations
import argparse, hashlib, inspect, json, re, shlex, subprocess, sys
from pathlib import Path
from typing import Any
DISH_ROOT=Path(__file__).resolve().parents[1]; REPO_ROOT=DISH_ROOT.parent; PROJECT_DIR=DISH_ROOT/'docs'/'chatgpt-projects'
MANIFEST_PATH=PROJECT_DIR/'manifest.json'; EVALS_PATH=PROJECT_DIR/'evals.json'; ROLE_INDEX_PATH=DISH_ROOT/'docs'/'agents'/'index.md'; ROOT_INSTRUCTIONS_PATH=REPO_ROOT/'CLAUDE.md'
STANDING_INVARIANTS_PATH=DISH_ROOT/'docs'/'agents'/'standing-invariants.json'
REPOSITORY_CONTEXT_ROLES=('audit','coordinator','development-workflow','implementation','integration','postgresql-dark-launch','review','workflow')
REPOSITORY_CONTEXT_EVAL_IDS=('repository-context-admission-consequential-reasoning','repository-context-admission-missing-bundle','repository-context-admission-reentry','repository-context-admission-stale-main','repository-context-admission-tiny-lookup','standing-policy-post-integration-main-readback')
REPOSITORY_CONTEXT_ADMISSION_ORDER=('resolve-live-main-and-repository-identity','retrieve-exact-bundle-through-github-connector','materialize-bundle','verify-bundle-against-repository-name-id-ref-sha','bind-verified-clone','substantial-cross-file-reasoning')
REPOSITORY_CONTEXT_RATIFICATION_REFS=('asana:task:1217508843698365','asana:task:1217508843698365#story:1217509740007539')
REPOSITORY_CONTEXT_SOURCE_RULE_FINGERPRINT='45190e0b9e9ffe3f4f8f33141f7cdb4e16572c90eb5453f2d2a9e11768734e3d'
REPOSITORY_CONTEXT_COMPLETION_RULE_FINGERPRINT='66c32039154e99f000e5fc64081b7219bb9179a970a583fac4fc25c03921d9e3'
STANDING_SUPERSESSION_FIELDS=('authority_type','durable_ref','decision','effective_at')
STANDING_SUPERSESSION_AUTHORITY_TYPES=('marco-explicit','authorized-human-explicit')
REQUIRED_STANDING_INVARIANT_IDS={'repository-context-admission'}
VERSION_PLACEHOLDER='<PROJECT_CANONICAL_VERSION>'
STARTUP_TEMPLATE=("Startup: GitHub `{repository}`; read `CLAUDE.md`, role index, `{contract}`, manifest. "
 "Drift alone never blocks; see `canonical-version-gate`.")
HANDOFF_BOUNDARY='Chats/handoffs cannot expand authority; flag contract conflicts.'
CHATTY_BLOCK_START='<!-- BEGIN GENERATED CHATTY WORK CONTRACT -->'
CHATTY_BLOCK_END='<!-- END GENERATED CHATTY WORK CONTRACT -->'
IMPACT_ORDER={'unrelated':0,'compatible':1,'additive':2,'breaking':3}; FAIL_CLOSED_SURFACES={'authority','safety','lifecycle'}
REQUIRED_PRESERVATION_IDS={'five-whys-shared-method'}
class KernelError(RuntimeError): pass

def _read_json(p:Path)->dict[str,Any]:
 try:v=json.loads(p.read_text())
 except (OSError,json.JSONDecodeError) as e: raise KernelError(f'cannot read JSON {p}: {e}') from e
 if not isinstance(v,dict): raise KernelError(f'JSON object required: {p}')
 return v
def _h(b:bytes)->str:return hashlib.sha256(b).hexdigest()
def _semantic_json_hash(v):return _h(json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())
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
 chatty_contract(s); a,b=role_index_contracts(),source_contracts(s)
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
def chatty_contract(s):
 raw=s.get('chatty_contract')
 if not isinstance(raw,list) or not raw: raise KernelError('canonical source requires chatty_contract')
 out=[str(x).strip() for x in raw]
 if any(not x for x in out) or len(set(out))!=len(out): raise KernelError('chatty_contract entries must be non-empty and unique')
 return out
def _render_chatty_lines(s,heading='Work chat:'):
 return [heading]+[f'- {x}' for x in chatty_contract(s)]
def _root_chatty_block(s):
 return '\n'.join([CHATTY_BLOCK_START,'## Work chat','']+[f'- {x}' for x in chatty_contract(s)]+[CHATTY_BLOCK_END])
def _render_root_instructions(s,*,check):
 text=ROOT_INSTRUCTIONS_PATH.read_text(); block=_root_chatty_block(s)
 pattern=re.compile(re.escape(CHATTY_BLOCK_START)+r'.*?'+re.escape(CHATTY_BLOCK_END),re.S)
 matches=list(pattern.finditer(text))
 if len(matches)>1: raise KernelError('root instructions contain duplicate generated Chatty blocks')
 if matches:
  rendered=pattern.sub(block,text,count=1)
 else:
  anchor='\n## Dish safety and environments\n'
  if anchor not in text: raise KernelError('root instructions missing Chatty insertion anchor')
  rendered=text.replace(anchor,'\n'+block+'\n'+anchor,1)
 if check:
  if text!=rendered: raise KernelError('generated root Chatty contract differs: CLAUDE.md')
 else: ROOT_INSTRUCTIONS_PATH.write_text(rendered)
 return len(block)
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
 lines += _render_context_dependencies(s,role)+['']+_render_chatty_lines(s)+['',f"Role: **{r['default_role']}**."]
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
 return _h('\0'.join((STARTUP_TEMPLATE,HANDOFF_BOUNDARY,CHATTY_BLOCK_START,CHATTY_BLOCK_END,inspect.getsource(chatty_contract),inspect.getsource(_render_chatty_lines),inspect.getsource(_root_chatty_block),inspect.getsource(_render_root_instructions),inspect.getsource(repository_config),inspect.getsource(context_dependencies),inspect.getsource(_render_context_dependencies),inspect.getsource(render_role_with_version),inspect.getsource(kernel_identity))).encode())
def _impact(c):
 x=str(c.get('impact','')).strip()
 if x not in {'compatible','additive','breaking'}: raise KernelError(f"explicit transition impact required for {c.get('rule_id','<unknown>')!r}")
 return x
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
def _proof_text(v,key):
 x=str(v.get(key,'')).strip() if isinstance(v,dict) else ''
 if not x: raise KernelError(f'BREAKING proof requires {key}')
 return x
def _validate_breaking(e,c):
 p=c.get('break_proof')
 if not isinstance(p,dict): raise KernelError(f"BREAKING {c.get('rule_id')} requires break_proof")
 if str(p.get('from_version',''))!=str(e.get('from_version')) or str(p.get('to_version',''))!=str(e.get('to_version')): raise KernelError(f"BREAKING {c.get('rule_id')} proof transition mismatch")
 if p.get('roles')!=c.get('roles') or p.get('action_boundaries')!=c.get('action_boundaries'): raise KernelError(f"BREAKING {c.get('rule_id')} proof scope mismatch")
 for key in ('prior_kernel_identity','counterexample','git_reconciliation_failure','migration','rollback','evidence_ref'): _proof_text(p,key)
 if c.get('marco_approved') is not True or not str(c.get('marco_approval_ref','')).strip(): raise KernelError(f"BREAKING {c.get('rule_id')} requires explicit Marco approval reference")
def _validate_correction(c):
 q=c.get('historical_correction')
 if q is None:return
 if not isinstance(q,dict) or q.get('previous_impact')!='breaking' or not str(q.get('reason','')).strip() or not str(q.get('provenance_ref','')).strip(): raise KernelError(f"invalid historical correction for {c.get('rule_id')}")
def _legacy_floor(m):
 floor=m.get('legacy_bootstrap_floor')
 if not isinstance(floor,dict): raise KernelError('manifest.legacy_bootstrap_floor required')
 first=str(floor.get('first_drift_aware_version','')).strip(); pre=floor.get('pre_floor_versions')
 if not first or not isinstance(pre,list) or not pre or any(not str(x).strip() for x in pre) or first in set(map(str,pre)): raise KernelError('invalid legacy bootstrap floor versions')
 if floor.get('impact')!='breaking' or floor.get('roles')!=['*'] or floor.get('action_boundaries')!=['*']: raise KernelError('legacy bootstrap floor must be global BREAKING')
 proof=floor.get('break_proof')
 if not isinstance(proof,dict): raise KernelError('legacy bootstrap floor requires break_proof')
 for key in ('prior_kernel_identity','counterexample','git_reconciliation_failure','migration','rollback','evidence_ref'): _proof_text(proof,key)
 if floor.get('marco_approved') is not True or not str(floor.get('marco_approval_ref','')).strip(): raise KernelError('legacy bootstrap floor requires Marco approval reference')
 return first,[str(x) for x in pre]
def validate_change_history(m,s,*,role_key=None,action_boundary=None):
 h=m.get('change_history'); roles=set(s['roles']); ids={x['id'] for r in roles for x in effective_rules(s,r)}
 scoped=role_key is not None or action_boundary is not None
 if scoped and (role_key not in roles or not str(action_boundary).strip()): raise KernelError('scoped history validation requires known role/action boundary')
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
 seen=set(); versions=set(); inbound={}
 for e in h:
  if not isinstance(e,dict): raise KernelError('change_history entries must be objects')
  a=str(e.get('from_version','')).strip(); b=str(e.get('to_version','')).strip(); ch=e.get('changes')
  if not a or not b or a==b or a in seen: raise KernelError(f'ambiguous/invalid change_history transition from {a!r}')
  seen.add(a); versions.update((a,b)); inbound[b]=inbound.get(b,0)+1
  if inbound[b]>1: raise KernelError(f'ambiguous change_history destination {b!r}')
  if not isinstance(ch,list) or not ch: raise KernelError(f'change_history {a}->{b} requires changes')
  change_seen=set()
  for c in ch:
   if not isinstance(c,dict): raise KernelError(f'change_history {a}->{b} contains non-object change')
   rs=c.get('roles'); bounds=c.get('action_boundaries')
   if not isinstance(rs,list) or not rs or any(not isinstance(x,str) or not x.strip() for x in rs) or (set(rs)!={'*'} and not set(rs).issubset(roles)): raise KernelError(f'change_history {c.get("rule_id")} has invalid roles')
   if not isinstance(bounds,list) or not bounds or any(not isinstance(x,str) or not x.strip() for x in bounds): raise KernelError(f'change_history {c.get("rule_id")} requires boundaries')
   if scoped and '*' not in rs and role_key not in rs: continue
   if scoped and '*' not in bounds and action_boundary not in bounds: continue
   rid=str(c.get('rule_id','')).strip(); surf=str(c.get('surface','')).strip()
   if not rid or (rid not in ids and not rid.startswith('renderer:')): raise KernelError(f'change_history references unknown rule {rid!r}')
   if not surf: raise KernelError(f'change_history {rid} requires surface')
   key=(rid,tuple(rs),tuple(bounds))
   if key in change_seen: raise KernelError(f'duplicate/conflicting change_history classification for {rid}')
   change_seen.add(key); imp=_impact(c); _validate_correction(c)
   if imp=='breaking': _validate_breaking(e,c)
 canonical=str(m.get('canonical_version',''))
 if canonical not in versions or not any(str(e.get('to_version'))==canonical for e in h): raise KernelError('change_history lacks canonical transition')
 first,pre=_legacy_floor(m)
 if first not in versions or any(v not in versions for v in pre): raise KernelError('legacy bootstrap floor references unknown retained version')
 if any(v==canonical for v in pre): raise KernelError('canonical version cannot be pre-floor')
 # d96+ history may never retain an unproved hard break; the proof validator above is the only admissibility path.
 _validate_current_edge_classification(m,s)
def _standing_invariant_registry():
 payload=_read_json(STANDING_INVARIANTS_PATH)
 if payload.get('schema_version')!=1 or not isinstance(payload.get('invariants'),list): raise KernelError('standing invariant registry schema mismatch')
 by={}
 for raw in payload['invariants']:
  if not isinstance(raw,dict): raise KernelError('standing invariant entries must be objects')
  rid=str(raw.get('id','')).strip()
  if not rid or rid in by: raise KernelError(f'invalid/duplicate standing invariant {rid!r}')
  by[rid]=raw
 missing=REQUIRED_STANDING_INVARIANT_IDS-set(by); extras=set(by)-REQUIRED_STANDING_INVARIANT_IDS
 if missing: raise KernelError(f'missing required standing invariants: {sorted(missing)}')
 if extras: raise KernelError(f'standing invariants lack independent validators: {sorted(extras)}')
 return by

def _validate_standing_supersession(entry):
 policy=entry.get('supersession_policy'); sup=entry.get('supersession')
 if not isinstance(policy,dict) or policy.get('durable_explicit_authority_required') is not True: raise KernelError(f"standing invariant {entry.get('id')} requires supersession policy")
 required=policy.get('required_fields'); allowed=policy.get('accepted_authority_types')
 if required!=list(STANDING_SUPERSESSION_FIELDS) or allowed!=list(STANDING_SUPERSESSION_AUTHORITY_TYPES): raise KernelError(f"standing invariant {entry.get('id')} supersession policy changed without authority")
 if not isinstance(sup,dict) or any(not str(sup.get(k,'')).strip() for k in STANDING_SUPERSESSION_FIELDS): raise KernelError(f"standing invariant {entry.get('id')} supersession requires durable explicit authority fields")
 if str(sup.get('authority_type')) not in STANDING_SUPERSESSION_AUTHORITY_TYPES: raise KernelError(f"standing invariant {entry.get('id')} supersession authority type is not accepted")

def validate_standing_invariants(s,*,registry=None,eval_ids=None,required_eval_ids=None):
 registry=_standing_invariant_registry() if registry is None else registry
 if not isinstance(registry,dict): raise KernelError('standing invariant registry must resolve to an object map')
 missing=REQUIRED_STANDING_INVARIANT_IDS-set(registry)
 if missing: raise KernelError(f'missing required standing invariants: {sorted(missing)}')
 entry=registry['repository-context-admission']; status=str(entry.get('status','')).strip()
 if status=='superseded':
  _validate_standing_supersession(entry); return ['repository-context-admission:superseded']
 if status!='active': raise KernelError('standing invariant repository-context-admission must be active or superseded')
 rat=entry.get('ratification')
 if not isinstance(rat,dict) or not str(rat.get('decision','')).strip() or not isinstance(rat.get('durable_authority_refs'),list) or not rat['durable_authority_refs'] or any(not str(x).strip() for x in rat['durable_authority_refs']): raise KernelError('standing invariant repository-context-admission requires durable ratification provenance')
 if not set(REPOSITORY_CONTEXT_RATIFICATION_REFS).issubset(set(map(str,rat['durable_authority_refs']))): raise KernelError('standing invariant repository-context-admission lost durable approval provenance')
 semantic=entry.get('semantic_contract')
 expected_semantic={'admission_order':list(REPOSITORY_CONTEXT_ADMISSION_ORDER),'tiny_targeted_reads_exempt':True,'reentry_events':['fresh-or-replacement-session','post-compaction-reground','affected-role-switch','main-movement-with-absent-or-stale-witness'],'failure_scope':'affected-substantial-conclusion-only','bundle_authority':'read-only-context','current_state_authorities':['GitHub','Asana']}
 if semantic!=expected_semantic: raise KernelError('standing invariant repository-context-admission semantic contract changed without supersession')
 coverage=entry.get('coverage')
 if not isinstance(coverage,dict): raise KernelError('standing invariant repository-context-admission requires coverage')
 if coverage.get('source_rule_id')!='repository-context-admission' or coverage.get('source_rule_fingerprint')!=REPOSITORY_CONTEXT_SOURCE_RULE_FINGERPRINT or set(coverage.get('required_eval_ids',[]))!=set(REPOSITORY_CONTEXT_EVAL_IDS) or set(coverage.get('rendered_roles',[]))!=set(REPOSITORY_CONTEXT_ROLES) or coverage.get('completion_role')!='integration' or coverage.get('completion_rule_id')!='integration-standing-policy-readback' or coverage.get('completion_rule_fingerprint')!=REPOSITORY_CONTEXT_COMPLETION_RULE_FINGERPRINT: raise KernelError('standing invariant repository-context-admission coverage weakened without supersession')
 shared={x['id']:x for x in _rules(s.get('shared_rules'),'shared_rules')}
 rule=shared.get('repository-context-admission')
 if rule is None: raise KernelError('standing invariant repository-context-admission missing canonical shared source rule')
 if REPOSITORY_CONTEXT_SOURCE_RULE_FINGERPRINT!=_rule_fingerprint(rule): raise KernelError('standing invariant repository-context-admission source rule differs from ratified fingerprint')
 if set(s.get('roles',{}))!=set(REPOSITORY_CONTEXT_ROLES): raise KernelError('standing invariant repository-context-admission rendered role topology mismatch')
 for role in REPOSITORY_CONTEXT_ROLES:
  if 'repository-context-admission' not in {x['id'] for x in effective_rules(s,role)}: raise KernelError(f'standing invariant repository-context-admission missing rendered role coverage for {role}')
  if rule['text'] not in render_role_with_version(s,role,VERSION_PLACEHOLDER): raise KernelError(f'standing invariant repository-context-admission not compiled into {role}')
 completion=next((x for x in _rules(s['roles']['integration'].get('rules'),'roles.integration.rules') if x['id']=='integration-standing-policy-readback'),None)
 if completion is None or REPOSITORY_CONTEXT_COMPLETION_RULE_FINGERPRINT!=_rule_fingerprint(completion): raise KernelError('standing invariant repository-context-admission completion rule missing or changed')
 actual_eval_ids=set(x['id'] for x in _evals()) if eval_ids is None else set(eval_ids)
 inventory=set(REQUIRED_EVAL_IDS) if required_eval_ids is None else set(required_eval_ids)
 expected=set(REPOSITORY_CONTEXT_EVAL_IDS)
 if not expected.issubset(actual_eval_ids): raise KernelError(f'standing invariant repository-context-admission missing required evals: {sorted(expected-actual_eval_ids)}')
 if not expected.issubset(inventory): raise KernelError(f'standing invariant repository-context-admission missing independent required-eval inventory: {sorted(expected-inventory)}')
 return ['repository-context-admission:active']

def generated_sha256(m,s):
 parts=[]
 for role in sorted(s['roles']): parts.append(role+'\0'+render_role_with_version(s,role,str(m['canonical_version'])))
 return _h('\0'.join(parts).encode())
def load_canonical(*,validate_history=True):
 m=_read_json(MANIFEST_PATH); p=PROJECT_DIR/str(m.get('source_file','')); s=_read_json(p)
 if m.get('source_sha256')!=_semantic_json_hash(s): raise KernelError('canonical source semantic hash mismatch')
 if s.get('schema_version')!=m.get('schema_version'): raise KernelError('manifest/source schema mismatch')
 kid=kernel_identity(s)
 if m.get('kernel_identity_sha256')!=kid: raise KernelError('rendered kernel identity mismatch')
 exp=f"{m.get('version_namespace','')}-{kid[:12]}"
 if m.get('canonical_version')!=exp: raise KernelError(f'canonical_version must be {exp!r}')
 if m.get('generated_sha256')!=generated_sha256(m,s): raise KernelError('current generated Project digest mismatch')
 if validate_history:validate_change_history(m,s)
 validate_standing_invariants(s)
 return m,s
def render_role(m,s,role):return render_role_with_version(s,role,str(m['canonical_version']))
def generated_paths(m,s):
 files=m.get('generated_role_files')
 if not isinstance(files,dict) or set(files)!=set(s['roles']): raise KernelError('generated role file map mismatch')
 return {r:PROJECT_DIR/str(files[r]) for r in s['roles']}
def render_all(*,check):
 m,s=load_canonical(); limit=int(m.get('max_kernel_chars',3500)); out=[]; _render_root_instructions(s,check=check)
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
def _integrity_result(project_version,canonical,role,boundary,error):
 return {'project_version':project_version,'canonical_version':canonical,'role':role,'action_boundary':boundary,'state':'integrity_error','impact':'integrity_error','drift_level':None,'indicator':'PROJECT SETTINGS: INTEGRITY ERROR · DRIFT ?/3','block':True,'resync_required':False,'settings_refresh_recommended':False,'repair':'repository-authority','error':str(error),'changes':[]}
def classify_project_drift(project_version,role_key,action_boundary,*,manifest=None,source=None,actual_generated_sha256=None):
 if manifest is None or source is None:
  try:mm,ss=load_canonical(validate_history=False)
  except KernelError as e:return _integrity_result(project_version,'<unknown>',role_key,str(action_boundary),e)
  manifest=manifest or mm; source=source or ss
 canonical=str(manifest.get('canonical_version',''))
 boundary=str(action_boundary).strip()
 if role_key not in source.get('roles',{}): return _integrity_result(project_version,canonical,role_key,boundary,'unknown Project role')
 if not boundary:return _integrity_result(project_version,canonical,role_key,boundary,'drift classification requires action_boundary')
 try:validate_change_history(manifest,source,role_key=role_key,action_boundary=boundary)
 except KernelError as e:return _integrity_result(project_version,canonical,role_key,boundary,e)
 if project_version==canonical:
  if actual_generated_sha256 is not None and str(actual_generated_sha256)!=str(manifest.get('generated_sha256','')): return _integrity_result(project_version,canonical,role_key,boundary,'current generated Project digest mismatch')
  return {'project_version':project_version,'canonical_version':canonical,'role':role_key,'action_boundary':boundary,'state':'current','impact':'unrelated','drift_level':0,'indicator':None,'block':False,'resync_required':False,'settings_refresh_recommended':False,'changes':[]}
 try:first,pre=_legacy_floor(manifest)
 except KernelError as e:return _integrity_result(project_version,canonical,role_key,boundary,e)
 if project_version in pre:
  return {'project_version':project_version,'canonical_version':canonical,'role':role_key,'action_boundary':boundary,'state':'legacy_hard_break','impact':'breaking','drift_level':3,'indicator':'PROJECT SETTINGS: HARD BREAK · DRIFT 3/3','block':True,'resync_required':True,'settings_refresh_recommended':False,'legacy_bootstrap_incompatibility':True,'first_drift_aware_version':first,'changes':[]}
 try:path=_change_path(manifest,project_version)
 except KernelError as e:return _integrity_result(project_version,canonical,role_key,boundary,e)
 effective=[]; blocking=[]
 for e in path:
  for raw in e['changes']:
   if '*' not in raw['roles'] and role_key not in raw['roles']: continue
   rel='*' in raw['action_boundaries'] or boundary in raw['action_boundaries']
   if not rel: continue
   c=dict(raw,from_version=e['from_version'],to_version=e['to_version']); c['impact']=_impact(c); effective.append(c)
   if c['impact']=='breaking': blocking.append(c)
 impact='breaking' if blocking else (max((x['impact'] for x in effective),key=IMPACT_ORDER.get) if effective else 'unrelated')
 level=3 if impact=='breaking' else (2 if impact=='additive' else 1)
 indicator='PROJECT SETTINGS: HARD BREAK · DRIFT 3/3' if level==3 else f'PROJECT SETTINGS: OUTDATED · DRIFT {level}/3'
 return {'project_version':project_version,'canonical_version':canonical,'role':role_key,'action_boundary':boundary,'state':'hard_break' if blocking else 'outdated','impact':impact,'drift_level':level,'indicator':indicator,'block':bool(blocking),'resync_required':bool(blocking),'settings_refresh_recommended':not blocking,'changes':effective}

REQUIRED_EVAL_IDS={'action-first-lifecycle-output', 'active-gate-blocker-cannot-be-deferred', 'additive-evidence-drift', 'allowed-specialist-implementation-composition', 'audit-dedupe-existing-finding', 'audit-exact-baseline', 'audit-missing-authority-fails-closed', 'audit-moved-baseline-current-blocker', 'audit-new-finding-backlog-only', 'audit-refuses-mutation-authority', 'audit-specialist-context-no-authority', 'authenticated-account-not-human-decision', 'chat-only-review-verdict-not-complete', 'chatty-authorized-action-before-narration', 'chatty-high-level-review-summary', 'chatty-progress-is-not-completion', 'chatty-session-correction-latches', 'chatty-status-reconciles-before-reroute', 'code-smell-dedupe-log-and-continue', 'code-smell-true-blocker-stays-active', 'comparison-incompatible-target-escalates-implementation', 'compatible-concise-output-drift', 'compatible-wording-drift', 'configured-repository-pr-routing', 'coordinator-check-everything-mixed-state', 'coordinator-pr-intake-automatic-review', 'cross-role-context-bleed', 'current-template-lookup', 'development-workflow-context-preload-no-authority', 'development-workflow-pr40-fallback-context', 'development-workflow-pr60-test-scope-context', 'disposable-fixture-still-needs-health', 'durable-review-classification', 'emergency-attach-after-review', 'emergency-attach-asana-authority-revoked', 'emergency-attach-branch-race', 'emergency-attach-conflicting-writer', 'emergency-attach-consumed-once', 'emergency-attach-eligible', 'emergency-attach-final-readback-required', 'emergency-attach-forbids-semantic-actions', 'emergency-attach-normal-broker-path-unchanged', 'emergency-attach-parent-mismatch', 'emergency-attach-policy-denial', 'emergency-attach-tree-mismatch', 'failed-ci-ownership-before-fix', 'five-whys-evidence-discipline', 'five-whys-reground-reload', 'forbidden-implicit-role-expansion', 'friction-active-blocker-routes-to-active-work', 'friction-dedupe-no-urgency', 'handoff-conflicts-with-role-authority', 'implementation-escalation-is-action-first', 'implementation-rejects-patch-only-completion', 'integration-bounded-reconciliation', 'integration-breaking-merge-drift', 'integration-rejects-head-mismatch', 'live-authority-over-stale-memory', 'mutation-broker-proof-required', 'no-valid-fallback', 'post-merge-asana-residual-gate', 'project-drift-current-silent', 'project-drift-integrity-error', 'project-drift-pre-d96-legacy', 'project-drift-self-compatible', 'project-drift-v708-review-compatible', 'publication-blocker-forbids-unsafe-shortcuts', 'publication-completion-invalidates-prior-review', 'publication-fully-published-local-certification', 'publication-handoff-before-human-notification', 'publication-materializer-eligible-blocker', 'publication-unsafe-governed-path-blocker', 'repository-context-admission-consequential-reasoning', 'repository-context-admission-missing-bundle', 'repository-context-admission-reentry', 'repository-context-admission-stale-main', 'repository-context-admission-tiny-lookup', 'repository-friction-discovery', 'review-breaking-completion-drift', 'review-exact-head-completion', 'reviewed-head-movement-classification', 'scope-amplification-checkpoint', 'separate-pr-does-not-clear-independent-blocker', 'shared-resource-concurrency-preflight', 'skipped-version-breaking-drift', 'skipped-version-nonbreaking-drift', 'stale-project-version', 'standing-policy-post-integration-main-readback', 'supported-operation-stays-local-system-access', 'task-history-before-no-op', 'unrelated-role-drift', 'valid-action-fallback'}
ORACLE_FIELDS={'expected','failure','expected_outcome','required_actions','forbidden_actions','required_observations','required_observations_by_role','require_ordered_observations','observation_link_field'}
def _eval_payload():return _read_json(EVALS_PATH)
def _evals():
 x=_eval_payload().get('scenarios');
 if not isinstance(x,list): raise KernelError('evals scenarios must be a list')
 return x
def validate_preservation_inventory(s,payload,preservation):
 if not isinstance(preservation,dict): raise KernelError('manifest requires preservation_inventory')
 if preservation.get('schema_version')!=1: raise KernelError('preservation inventory schema_version must be 1')
 entries=preservation.get('entries')
 if not isinstance(entries,list): raise KernelError('preservation inventory entries must be a list')
 by={str(x.get('id','')).strip():x for x in entries if isinstance(x,dict)}
 if set(by)!=REQUIRED_PRESERVATION_IDS: raise KernelError(f'preservation inventory mismatch missing={sorted(REQUIRED_PRESERVATION_IDS-set(by))} extras={sorted(set(by)-REQUIRED_PRESERVATION_IDS)}')
 scenarios=payload.get('scenarios')
 if not isinstance(scenarios,list): raise KernelError('evals scenarios must be a list')
 scenario_by={str(x.get('id','')).strip():x for x in scenarios if isinstance(x,dict)}
 shared={x['id']:x for x in _rules(s.get('shared_rules'),'shared_rules')}
 for pid,e in by.items():
  canonical=_dependency_path(e.get('canonical_document'),f'preservation.{pid}.canonical_document')
  index_document=_dependency_path(e.get('index_document'),f'preservation.{pid}.index_document')
  link=str(e.get('index_link','')).strip(); index_text=(REPO_ROOT/index_document).read_text()
  if not link or link not in index_text: raise KernelError(f'preservation {pid} index link missing from {index_document}')
  rid=str(e.get('shared_rule_id','')).strip()
  if rid!=pid or rid not in shared: raise KernelError(f'preservation {pid} shared rule missing from canonical source')
  if canonical not in shared[rid]['text']: raise KernelError(f'preservation {pid} shared rule does not point to {canonical}')
  if e.get('roles')!=['*']: raise KernelError(f'preservation {pid} must cover every Project role')
  for role in s['roles']:
   if rid not in {x['id'] for x in effective_rules(s,role)}: raise KernelError(f'preservation {pid} shared rule missing from role {role}')
  behavior=e.get('behavior_scenario_ids')
  if not isinstance(behavior,list) or not behavior or len(behavior)!=len(set(behavior)): raise KernelError(f'preservation {pid} requires unique behavior_scenario_ids')
  for sid in behavior:
   q=scenario_by.get(str(sid))
   if q is None: raise KernelError(f'preservation {pid} behavior scenario missing: {sid}')
   if rid not in q.get('required_rules',[]): raise KernelError(f'preservation {pid} behavior scenario {sid} does not require {rid}')
   if set(q.get('roles',[]))!=set(s['roles']): raise KernelError(f'preservation {pid} behavior scenario {sid} must cover every Project role')
 return sorted(by)
def _obs_pattern(p,sid):
 if not isinstance(p,dict) or not str(p.get('kind','')).strip() or not str(p.get('operation','')).strip(): raise KernelError(f'eval {sid} invalid observation pattern')
 if 'equals' in p and not isinstance(p['equals'],dict): raise KernelError(f'eval {sid} observation equals must be object')
def validate_eval_contracts():
 m,s=load_canonical(); payload=_eval_payload(); validate_preservation_inventory(s,payload,m.get('preservation_inventory')); default_expected=str(payload.get('default_expected','')).strip(); default_failure=str(payload.get('default_failure','')).strip(); seen=set(); out=[]
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
 d=classify_project_drift(project_version,role_key,action_boundary)
 if d['state']=='current':return True,''
 if d['state']=='integrity_error':return False,f"{d['indicator']} — repair repository authority before this action; Project resync is not the repair"
 if d['block']:return False,f"{d['indicator']} — project={project_version} repository={d['canonical_version']} role={role_key} boundary={action_boundary}; scheduled Project migration/resync required"
 suffix='; refresh Project settings when convenient' if d['drift_level']==2 else ''
 return True,f"{d['indicator']} — continue under current repository authority{suffix}"
def command_check():
 m,s=load_canonical(); validate_topology(s); standing=validate_standing_invariants(s); rr=render_all(check=True); ee=validate_eval_contracts(); print(f"canonical_version={m['canonical_version']}"); print(f"kernel_identity_sha256={m['kernel_identity_sha256']}")
 for x in standing: print(f'PASS standing-invariant {x}')
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
