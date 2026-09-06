#!/usr/bin/env python3
"""Render/version/evaluate canonical ChatGPT Project kernels."""
from __future__ import annotations
import argparse, copy, hashlib, inspect, json, re, shlex, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
DISH_ROOT=Path(__file__).resolve().parents[1]; REPO_ROOT=DISH_ROOT.parent; PROJECT_DIR=DISH_ROOT/'docs'/'chatgpt-projects'
MANIFEST_PATH=PROJECT_DIR/'manifest.json'; EVALS_PATH=PROJECT_DIR/'evals.json'; ROLE_INDEX_PATH=DISH_ROOT/'docs'/'agents'/'index.md'; ROOT_INSTRUCTIONS_PATH=REPO_ROOT/'CLAUDE.md'
STANDING_INVARIANTS_PATH=DISH_ROOT/'docs'/'agents'/'standing-invariants.json'
FAST_TRACK_GATE_REGISTRY_PATH=PROJECT_DIR/'fast-track-gates.json'
CLAUDE_OPERATOR_STYLE_PATH=REPO_ROOT/'.claude'/'output-styles'/'dish-operator.md'
FAST_TRACK_OVERLAY_VERSION='fasttrack-r3'
FAST_TRACK_OVERLAY_HEADER='MARCO OVERRIDE — FAST-TRACK PROCESS'
PROJECT_SETTINGS_INITIAL_COMPATIBILITY_CHARS=8000
PROJECT_SETTINGS_CHANGE_EVIDENCE=('empirical-project-save-load-readback','official-project-limit')
REPOSITORY_CONTEXT_ROLES=('audit','coordinator','development-workflow','implementation','integration','postgresql-dark-launch','review','workflow')
REPOSITORY_CONTEXT_EVAL_IDS=('repository-context-admission-consequential-reasoning','repository-context-admission-missing-bundle','repository-context-admission-reentry','repository-context-admission-stale-main','repository-context-admission-tiny-lookup','standing-policy-post-integration-main-readback')
REPOSITORY_CONTEXT_ADMISSION_ORDER=('resolve-live-main-and-repository-identity','retrieve-exact-bundle-through-github-connector','materialize-bundle','verify-bundle-against-repository-name-id-ref-sha','bind-verified-clone','substantial-cross-file-reasoning')
REPOSITORY_CONTEXT_RATIFICATION_REFS=('asana:task:1217508843698365','asana:task:1217508843698365#story:1217509740007539','asana:task:1217594495187308')
REPOSITORY_CONTEXT_SOURCE_RULE_FINGERPRINT='bef91c9d40a0db7630c541da9af51a8642f458d8a4781f9a6bc53de4d597b16e'
REPOSITORY_CONTEXT_COMPLETION_RULE_FINGERPRINT='66c32039154e99f000e5fc64081b7219bb9179a970a583fac4fc25c03921d9e3'
STANDING_SUPERSESSION_FIELDS=('authority_type','durable_ref','decision','effective_at')
STANDING_SUPERSESSION_AUTHORITY_TYPES=('marco-explicit','authorized-human-explicit')
REQUIRED_STANDING_INVARIANT_IDS={'repository-context-admission'}
VERSION_PLACEHOLDER='<PROJECT_CANONICAL_VERSION>'
STARTUP_TEMPLATE=("Startup: resolve GitHub `{repository}` `{branch}`; fetch this role's current generated Project kernel, then read `CLAUDE.md`, role index, `{contract}`, and manifest from that same current Git. Installed Project text is bootstrap/version witness after grounding. Drift alone never blocks; see `canonical-version-gate`.")
HANDOFF_BOUNDARY='Chats/handoffs cannot expand authority; flag contract conflicts.'
CHATTY_BLOCK_START='<!-- BEGIN GENERATED CHATTY WORK CONTRACT -->'
CHATTY_BLOCK_END='<!-- END GENERATED CHATTY WORK CONTRACT -->'
CLAUDE_OPERATOR_BLOCK_START='<!-- BEGIN GENERATED DISH OPERATOR ATTENTION CONTRACT -->'
CLAUDE_OPERATOR_BLOCK_END='<!-- END GENERATED DISH OPERATOR ATTENTION CONTRACT -->'
DESIGN_BLOCK_START='<!-- BEGIN GENERATED DESIGN PRINCIPLES BOOTSTRAP -->'
DESIGN_BLOCK_END='<!-- END GENERATED DESIGN PRINCIPLES BOOTSTRAP -->'
IMPACT_ORDER={'unrelated':0,'compatible':1,'additive':2,'breaking':3}; FAIL_CLOSED_SURFACES={'authority','safety','lifecycle'}
REQUIRED_PRESERVATION_IDS={'five-whys-shared-method','design-principles-bootstrap'}
REQUIRED_VERSION_INVENTORY_SCHEMA=1
class KernelError(RuntimeError): pass
class ProjectSettingsOverflow(KernelError):
 def __init__(self,report):
  self.report=report
  super().__init__(
   f"Project settings overflow role={report['role']} channel={report['channel']} "
   f"base_kernel_chars={report['base_kernel_chars']} test_metadata_delta_chars={report['test_metadata_delta_chars']} "
   f"overlay_chars={report['overlay_chars']} total_chars={report['total_chars']} "
   f"ceiling_chars={report['max_project_settings_chars']} excess_chars={report['excess_chars']}"
  )

def _read_json(p:Path)->dict[str,Any]:
 try:v=json.loads(p.read_text())
 except (OSError,json.JSONDecodeError) as e: raise KernelError(f'cannot read JSON {p}: {e}') from e
 if not isinstance(v,dict): raise KernelError(f'JSON object required: {p}')
 return v
def _read_manifest(p:Path)->dict[str,Any]:
 m=_read_json(p); shards=m.pop('change_history_shards',None)
 if shards is None:return m
 if 'change_history' in m or not isinstance(shards,list) or not shards:
  raise KernelError(f'invalid change_history shard index: {p}')
 history=[]
 for raw in shards:
  shard=Path(str(raw))
  if shard.is_absolute() or '..' in shard.parts:raise KernelError(f'unsafe change_history shard path: {raw!r}')
  try:value=json.loads((p.parent/shard).read_text())
  except (OSError,json.JSONDecodeError) as e:raise KernelError(f'cannot read change_history shard {p.parent/shard}: {e}') from e
  if not isinstance(value,list):raise KernelError(f'change_history shard must be a JSON array: {p.parent/shard}')
  history.extend(value)
 m['change_history']=history; return m
def _h(b:bytes)->str:return hashlib.sha256(b).hexdigest()
def _semantic_json_hash(v):return _h(json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())

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

def parse_fast_track_overlay_block(text):
 raw=str(text)
 if raw.count(FAST_TRACK_OVERLAY_HEADER)!=1: raise KernelError('Project settings must contain exactly one fast-track reserved header')
 tail=raw.split(FAST_TRACK_OVERLAY_HEADER,1)[1].lstrip()
 try:value,end=json.JSONDecoder().raw_decode(tail)
 except json.JSONDecodeError as e: raise KernelError(f'invalid fast-track overlay JSON: {e}') from e
 return canonical_fast_track_overlay(value)

def fast_track_overlay_digest(value): return 'sha256:'+_semantic_json_hash(canonical_fast_track_overlay(value))

def render_fast_track_overlay_block(value):
 overlay=canonical_fast_track_overlay(value)
 payload={k:overlay[k] for k in ('version','state','generation','scope','gate_semantics')}
 if overlay['expiry'] is not None: payload['expiry']=overlay['expiry']
 if overlay['reason']: payload['reason']=overlay['reason']
 return FAST_TRACK_OVERLAY_HEADER+'\n'+json.dumps(payload,ensure_ascii=False,separators=(',',':'))

def project_settings_compatibility_overlay():
 registry=fast_track_gate_registry()
 if not registry: raise KernelError('fast-track compatibility fixture requires a current gate')
 gid=sorted(registry)[0]; gate=registry[gid]; scope=f"{gid}@{gate['current_version']}"
 return {'version':FAST_TRACK_OVERLAY_VERSION,'state':'ACTIVE','generation':'g1','scope':[scope],'gate_semantics':{scope:gate['semantic_digest']},'expiry':None,'reason':''}

def project_settings_policy(manifest):
 if 'max_kernel_chars' in manifest: raise KernelError('manifest.max_kernel_chars is retired; use max_project_settings_chars')
 limit=manifest.get('max_project_settings_chars'); provenance=manifest.get('project_settings_compatibility')
 if not isinstance(limit,int) or limit<=0: raise KernelError('manifest.max_project_settings_chars must be a positive integer')
 if not isinstance(provenance,dict) or provenance.get('schema_version')!=1: raise KernelError('manifest.project_settings_compatibility schema_version must be 1')
 if provenance.get('qualified_chars')!=limit: raise KernelError('Project settings compatibility provenance must bind max_project_settings_chars exactly')
 basis=str(provenance.get('basis','')).strip(); evidence=str(provenance.get('evidence_ref','')).strip()
 if not evidence: raise KernelError('Project settings compatibility provenance requires evidence_ref')
 if provenance.get('change_evidence_required')!=list(PROJECT_SETTINGS_CHANGE_EVIDENCE): raise KernelError('Project settings compatibility change evidence policy mismatch')
 if limit==PROJECT_SETTINGS_INITIAL_COMPATIBILITY_CHARS:
  if basis!='existing-repository-budget' or provenance.get('vendor_contract') is not False: raise KernelError('initial Project settings compatibility budget must remain an explicit non-vendor repository budget')
 elif basis not in PROJECT_SETTINGS_CHANGE_EVIDENCE:
  raise KernelError('changing max_project_settings_chars requires empirical Project save/load/readback or official Project-limit evidence')
 return limit

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
 scope_key=f'{gid}@{gate_version}'
 if scope_key not in overlay['scope']: raise KernelError('fast-track gate is outside captured overlay scope')
 authorized_semantic_digest=overlay['gate_semantics'][scope_key]
 if authorized_semantic_digest!=gate['semantic_digest']: raise KernelError('fast-track gate is unknown, stale, or materially changed')
 task=str(task).strip(); candidate=str(candidate).strip(); action=str(action).strip(); raw_evidence=str(raw_evidence).strip()
 if not task or not candidate or not action or not raw_evidence: raise KernelError('fast-track use requires exact task/candidate/action/raw evidence')
 return {'marker':'GATE WAIVED BY MARCO OVERRIDE','overlay_generation':overlay['generation'],'overlay_digest':fast_track_overlay_digest(overlay),'gate_id':gid,'gate_version':gate_version,'gate_semantic_digest':authorized_semantic_digest,'task':task,'candidate':candidate,'action':action,'raw_evidence':raw_evidence}

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
def _dependency_locator(raw,label):
 value=str(raw).strip()
 if '#' not in value: raise KernelError(f'{label} must target an exact bounded ## section: {value!r}')
 path,heading=value.split('#',1); path=_dependency_path(path,label); heading=heading.strip()
 if not heading: raise KernelError(f'{label} requires an exact ## section heading')
 text=(REPO_ROOT/path).read_text()
 if f'## {heading}' not in text.splitlines(): raise KernelError(f'{label} section does not exist: {value!r}')
 return f'{path}#{heading}'
def _trigger_map(raw,label):
 if raw is None:return {}
 if not isinstance(raw,dict): raise KernelError(f'{label} must be an object')
 out={}
 for boundary,locators in raw.items():
  key=str(boundary).strip()
  if not key or not isinstance(locators,list) or not locators: raise KernelError(f'{label} entries require a label and bounded destinations')
  out[key]=[_dependency_locator(x,f'{label}.{key}') for x in locators]
 return out
def context_dependencies(s,role):
 shared=s.get('context_dependencies',{}); raw=s['roles'][role].get('context_dependencies',{})
 if not isinstance(shared,dict) or not isinstance(raw,dict): raise KernelError('context_dependencies must be objects')
 triggered={}
 for origin,label in ((shared,'context_dependencies.triggered_reads'),(raw,f'roles.{role}.context_dependencies.triggered_reads')):
  for key,paths in _trigger_map(origin.get('triggered_reads'),label).items():
   if key in triggered and triggered[key]!=paths: raise KernelError(f'conflicting triggered read {key!r} for {role}')
   triggered[key]=paths
 preload=raw.get('preload')
 normalized_preload=None
 if preload is not None:
  if not isinstance(preload,dict) or preload.get('role_index_contracts') is not True: raise KernelError(f'roles.{role}.context_dependencies.preload must require role_index_contracts')
  additional=preload.get('additional')
  if not isinstance(additional,list) or not additional: raise KernelError(f'roles.{role}.context_dependencies.preload.additional must be a non-empty list')
  normalized_preload={'role_index_contracts':True,'additional':[_dependency_path(x,f'roles.{role}.context_dependencies.preload.additional') for x in additional]}
 legacy=raw.get('action_specific')
 if legacy is not None:
  if not isinstance(legacy,dict): raise KernelError(f'roles.{role}.context_dependencies.action_specific must be an object')
  for key,paths in legacy.items():
   key=str(key).strip()
   if not key or not isinstance(paths,list) or not paths: raise KernelError(f'roles.{role}.context_dependencies.action_specific entries require a label and paths')
   # Legacy whole-file dependencies remain valid read-only context until migrated to bounded locators.
   triggered.setdefault(key,[_dependency_path(x,f'roles.{role}.context_dependencies.action_specific.{key}') for x in paths])
 if normalized_preload is None and not triggered:return None
 return {'preload':normalized_preload,'triggered_reads':triggered}
def validate_topology(s):
 chatty_contract(s); a,b=role_index_contracts(),source_contracts(s)
 if a!=b: raise KernelError(f'Project topology differs from role index: index={sorted(a)} source={sorted(b)}')
 for role in s['roles']: context_dependencies(s,role)
 profile_specs(s)
def profile_specs(s):
 raw=s.get('profiles',{})
 if not isinstance(raw,dict): raise KernelError('canonical source profiles must be an object')
 out={}
 for key,value in raw.items():
  if not re.fullmatch(r'[a-z0-9][a-z0-9-]*',str(key)) or not isinstance(value,dict): raise KernelError(f'invalid Project profile {key!r}')
  project_name=str(value.get('project_name','')).strip(); profile_id=str(value.get('profile_id','')).strip(); body=str(value.get('body','')).strip(); limit=value.get('max_chars',8000)
  if not project_name or not profile_id or not body or not isinstance(limit,int) or limit<=0: raise KernelError(f'Project profile {key!r} requires project_name/profile_id/body/max_chars')
  out[str(key)]={'project_name':project_name,'profile_id':profile_id,'body':body,'max_chars':limit}
 return out

def render_profile_with_version(s,key,version):
 profiles=profile_specs(s)
 if key not in profiles: raise KernelError(f'unknown Project profile {key!r}')
 spec=profiles[key]; repo,branch,_=repository_config(s)
 lines=[f"# {spec['project_name']}",'',f"PROFILE: {spec['profile_id']}",f'PROJECT_CANONICAL_VERSION: {version}','PROJECT_CHANNEL: production','CANONICAL_MANIFEST: dish/docs/chatgpt-projects/manifest.json',f'PROJECT_REPOSITORY: {repo}',f'PROJECT_DEFAULT_BRANCH: {branch}','',spec['body'],'']
 return '\n'.join(lines)

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
  delivery=x.get('delivery')
  if not isinstance(delivery,dict): raise KernelError(f'{label} rule {rid} requires delivery classification')
  mode=str(delivery.get('mode','')).strip()
  if mode not in {'DIRECT_ALWAYS_ON','TRIGGERED_READ'}: raise KernelError(f'{label} rule {rid} has invalid delivery mode')
  trigger=str(delivery.get('trigger','')).strip() if mode=='TRIGGERED_READ' else ''
  if mode=='TRIGGERED_READ' and not trigger: raise KernelError(f'{label} rule {rid} requires delivery.trigger')
  if mode=='DIRECT_ALWAYS_ON' and delivery.get('trigger'): raise KernelError(f'{label} rule {rid} DIRECT_ALWAYS_ON cannot name a trigger')
  seen.add(rid); out.append({'id':rid,'text':text,'impact':impact,'surface':surface,'action_boundaries':[str(z).strip() for z in bounds],'delivery':{'mode':mode,**({'trigger':trigger} if trigger else {})}})
 return out
def chatty_contract(s):
 raw=s.get('chatty_contract')
 if not isinstance(raw,list) or not raw: raise KernelError('canonical source requires chatty_contract')
 out=[str(x).strip() for x in raw]
 if any(not x for x in out) or len(set(out))!=len(out): raise KernelError('chatty_contract entries must be non-empty and unique')
 return out
def design_principles_rule(s):
 cfg=s.get('design_principles')
 if not isinstance(cfg,dict): raise KernelError('canonical source requires design_principles')
 canonical=_dependency_path(cfg.get('canonical_document'),'design_principles.canonical_document')
 if canonical!='dish/docs/agents/design-principles.md': raise KernelError('design_principles canonical document must be dish/docs/agents/design-principles.md')
 raw=(REPO_ROOT/canonical).read_bytes(); expected=str(cfg.get('sha256','')).strip()
 if not expected or _h(raw)!=expected: raise KernelError('design_principles canonical document digest mismatch')
 ids=cfg.get('principle_ids')
 if not isinstance(ids,list) or len(ids)!=11 or len(set(ids))!=11 or any(not re.fullmatch(r'DP-\d{2}',str(x)) for x in ids): raise KernelError('design_principles requires eleven stable DP-NN principle_ids')
 text=raw.decode(); found={}
 for match in re.finditer(r'^## (DP-\d{2}) — .+?\n\n\*\*Bootstrap:\*\* (.+)$',text,re.M):
  found[match.group(1)]=match.group(2).strip().rstrip('.')
 if list(found)!=list(map(str,ids)): raise KernelError(f'design_principles IDs/bootstrap mismatch: expected={ids} actual={list(found)}')
 projection=f'Design Principles ({Path(canonical).name}): '+'; '.join(f'{pid} {found[pid]}' for pid in ids)+'.'
 rid=str(cfg.get('shared_rule_id','')).strip(); impact=str(cfg.get('impact','')).strip(); surface=str(cfg.get('surface','')).strip(); bounds=cfg.get('action_boundaries')
 rule={'id':rid,'text':projection,'impact':impact,'surface':surface,'action_boundaries':bounds,'delivery':{'mode':'DIRECT_ALWAYS_ON'}}
 return _rules([rule],'design_principles')[0]
def shared_rules(s):
 out=[design_principles_rule(s)]+_rules(s.get('shared_rules'),'shared_rules'); ids=[x['id'] for x in out]
 if len(ids)!=len(set(ids)): raise KernelError('duplicate canonical shared rule id')
 return out
def _design_principles_block(s):
 rule=design_principles_rule(s)
 return '\n'.join([DESIGN_BLOCK_START,'## Critical Design Principles','',f"Generated projection of [`design-principles.md`](design-principles.md); canonical detail remains in that document.",'',rule['text'],DESIGN_BLOCK_END])
def _render_role_index_design_principles(s,*,check):
 text=ROLE_INDEX_PATH.read_text(); block=_design_principles_block(s)
 pattern=re.compile(re.escape(DESIGN_BLOCK_START)+r'.*?'+re.escape(DESIGN_BLOCK_END),re.S); matches=list(pattern.finditer(text))
 if len(matches)>1: raise KernelError('role index contains duplicate generated Design Principles blocks')
 if matches: rendered=pattern.sub(block,text,count=1)
 else:
  anchor='\n## Shared analysis methods\n'
  if anchor not in text: raise KernelError('role index missing Design Principles insertion anchor')
  rendered=text.replace(anchor,'\n'+block+'\n'+anchor,1)
 if check:
  if text!=rendered: raise KernelError('generated Design Principles bootstrap differs: role index')
 else: ROLE_INDEX_PATH.write_text(rendered)
 return len(block)
def _render_chatty_lines(s,heading='Work chat:'):
 return [heading]+[f'- {x}' for x in chatty_contract(s)]
def _render_project_chatty_lines(s):
 chatty_contract(s)
 return ['Work chat: after mandatory startup, apply root `CLAUDE.md` `## Work chat`; until grounded, be concise and lead with result/action/blocker/decision.']
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
def _render_claude_operator_style(s,*,check):
 text=CLAUDE_OPERATOR_STYLE_PATH.read_text()
 block='\n'.join([CLAUDE_OPERATOR_BLOCK_START,'## Canonical attention contract','',"This generated delivery surface consumes `dish/docs/chatgpt-projects/source.json`; it is not an independent communication authority.",'']+[f'- {x}' for x in chatty_contract(s)]+[CLAUDE_OPERATOR_BLOCK_END])
 pattern=re.compile(re.escape(CLAUDE_OPERATOR_BLOCK_START)+r'.*?'+re.escape(CLAUDE_OPERATOR_BLOCK_END),re.S); matches=list(pattern.finditer(text))
 if len(matches)>1: raise KernelError('Claude operator style contains duplicate generated attention blocks')
 if matches: rendered=pattern.sub(block,text,count=1)
 else:
  anchor='\n## Operator level\n'
  if anchor not in text: raise KernelError('Claude operator style missing attention insertion anchor')
  rendered=text.replace(anchor,'\n'+block+'\n'+anchor,1)
 if check:
  if text!=rendered: raise KernelError('generated Claude operator attention contract differs')
 else: CLAUDE_OPERATOR_STYLE_PATH.write_text(rendered)
 return len(block)
def repository_config(s):
 repo=str(s.get('repository_full_name','')).strip(); branch=str(s.get('default_branch','')).strip(); transport=str(s.get('github_transport','')).strip()
 if not repo or repo.count('/')!=1: raise KernelError('canonical source requires repository_full_name in owner/name form')
 if not branch: raise KernelError('canonical source requires default_branch')
 if not transport: raise KernelError('canonical source requires github_transport')
 return repo,branch,transport
def effective_rules(s,role):
 rs=shared_rules(s)+_rules(s['roles'][role].get('rules'),f'roles.{role}.rules'); ids=[x['id'] for x in rs]
 if len(ids)!=len(set(ids)): raise KernelError(f'duplicate effective rules for {role}')
 return rs
def _render_trigger_destinations(paths):
 out=[]; last=None
 for locator in paths:
  if '#' in locator:
   path,heading=locator.split('#',1)
   if path==last: out.append(f'#{heading}')
   else: out.append(locator); last=path
  else: out.append(locator); last=None
 return ' + '.join(f'`{x}`' for x in out)

def _render_context_dependencies(s,role):
 deps=context_dependencies(s,role)
 if deps is None:return []
 lines=[]
 preload=deps.get('preload')
 if preload:
  extra=' + '.join(f'`{x}`' for x in preload['additional'])
  lines.append(f'Startup/re-ground context: role-index standing contracts + {extra}. Read-only; grants no role/mutation/Review/Integration/merge/production authority.')
 triggers=deps.get('triggered_reads',{})
 used={}
 for rule in effective_rules(s,role):
  d=rule['delivery']
  if d['mode']=='TRIGGERED_READ': used.setdefault(d['trigger'],[]).append(rule['id'])
 missing=sorted(set(used)-set(triggers))
 if missing: raise KernelError(f'{role} triggered rules lack context destinations: {missing}')
 if triggers:
  lines.append('Triggered policy reads (before the governed action):')
  for label,paths in triggers.items():
   lines.append(f'- {label} -> {_render_trigger_destinations(paths)}')
 return lines
def render_role_with_version(s,role,version):
 r=s['roles'][role]; comps=r.get('allowed_compositions',[]); repo,branch,_=repository_config(s)
 if not isinstance(comps,list): raise KernelError(f'roles.{role}.allowed_compositions must be a list')
 lines=[f"# {r['project_name']}",'',f"PROJECT_ROLE: {r['default_role']}",f'PROJECT_CANONICAL_VERSION: {version}','PROJECT_CHANNEL: production','CANONICAL_MANIFEST: dish/docs/chatgpt-projects/manifest.json',f"ROLE_CONTRACT: {r['contract']}",f'PROJECT_REPOSITORY: {repo}',f'PROJECT_DEFAULT_BRANCH: {branch}','',STARTUP_TEMPLATE.format(repository=repo,branch=branch,contract=r['contract'])]
 lines += _render_context_dependencies(s,role)+['']+_render_project_chatty_lines(s)+['',f"Role: **{r['default_role']}**."]
 if comps: lines+=['Allowed composition only when explicitly triggered by current authority:']+[f'- {x}' for x in comps]
 else: lines+=['No implicit role composition is permitted.']
 direct=[x for x in effective_rules(s,role) if x['delivery']['mode']=='DIRECT_ALWAYS_ON']
 lines += [HANDOFF_BOUNDARY,'','High-consequence rules:']+[f"- {x['text']}" for x in direct]+['']
 return '\n'.join(lines)
def _render_test_candidate_kernel(s,role,*,candidate_version,pr_number,candidate_ref,candidate_head,candidate_manifest_sha256,production_version):
 if role not in s.get('roles',{}): raise KernelError(f'unknown role {role!r}')
 version=str(candidate_version).strip(); ref=str(candidate_ref).strip(); head=str(candidate_head).strip(); manifest_sha=str(candidate_manifest_sha256).strip(); prod=str(production_version).strip()
 if not version or not prod or not str(pr_number).isdigit() or not ref or not re.fullmatch(r'[0-9a-f]{40}',head) or not re.fullmatch(r'[0-9a-f]{64}',manifest_sha): raise KernelError('TEST candidate requires exact version/PR/ref/40-hex head/64-hex manifest identity')
 text=render_role_with_version(s,role,version)
 text=text.replace('PROJECT_CHANNEL: production',f'PROJECT_CHANNEL: test\nPROJECT_PRODUCTION_VERSION: {prod}\nPROJECT_CANDIDATE_PR: {pr_number}\nPROJECT_CANDIDATE_REF: {ref}\nPROJECT_CANDIDATE_HEAD: {head}\nPROJECT_CANDIDATE_MANIFEST_SHA256: {manifest_sha}',1)
 startup=STARTUP_TEMPLATE.format(repository=repository_config(s)[0],branch=repository_config(s)[1],contract=s['roles'][role]['contract'])
 test_startup=(f'Startup: resolve TEST candidate `{ref}` at exact head `{head}` and verify candidate manifest `{manifest_sha}` before using candidate instruction behavior. '
  f'Current production role/source authority remains the ceiling for genuine work; never chase a moved TEST head or treat TEST acceptance as production promotion.')
 if startup not in text: raise KernelError('TEST candidate startup replacement failed')
 return text.replace(startup,test_startup,1)

def render_project_settings_payload(m,s,role,*,channel='production',overlay=None,candidate_version=None,pr_number=None,candidate_ref=None,candidate_head=None,candidate_manifest_sha256=None,production_version=None):
 limit=project_settings_policy(m)
 if role not in s.get('roles',{}): raise KernelError(f'unknown role {role!r}')
 if channel=='production':
  base=render_role_with_version(s,role,str(m['canonical_version'])); base_kernel_chars=len(base); test_delta=0
 elif channel=='test':
  identity={'candidate_version':candidate_version,'pr_number':pr_number,'candidate_ref':candidate_ref,'candidate_head':candidate_head,'candidate_manifest_sha256':candidate_manifest_sha256,'production_version':production_version}
  base_kernel=render_role_with_version(s,role,str(candidate_version).strip())
  base=_render_test_candidate_kernel(s,role,**identity); base_kernel_chars=len(base_kernel); test_delta=len(base)-base_kernel_chars
 else: raise KernelError(f'unsupported Project settings channel {channel!r}')
 text=base; overlay_chars=0
 if overlay is not None:
  block=render_fast_track_overlay_block(overlay); suffix='\n'+block; text+=suffix; overlay_chars=len(suffix)
 total=len(text); remaining=limit-total; report={
  'text':text,'role':role,'channel':channel,'base_kernel_chars':base_kernel_chars,'test_metadata_delta_chars':test_delta,
  'overlay_chars':overlay_chars,'total_chars':total,'max_project_settings_chars':limit,
  'remaining_chars':max(remaining,0),'excess_chars':max(-remaining,0),
 }
 if total>limit: raise ProjectSettingsOverflow(report)
 return report

def render_test_candidate(s,role,*,candidate_version,pr_number,candidate_ref,candidate_head,candidate_manifest_sha256,production_version,manifest=None,overlay=None):
 m=_read_manifest(MANIFEST_PATH) if manifest is None else manifest
 return render_project_settings_payload(m,s,role,channel='test',overlay=overlay,candidate_version=candidate_version,pr_number=pr_number,candidate_ref=candidate_ref,candidate_head=candidate_head,candidate_manifest_sha256=candidate_manifest_sha256,production_version=production_version)['text']

def kernel_identity(s):
 repository_config(s); b=bytearray()
 for role in sorted(s['roles']):
  b+=role.encode()+b'\0'+render_role_with_version(s,role,VERSION_PLACEHOLDER).encode()+b'\0'
  md=[{k:x[k] for k in ('id','impact','surface','action_boundaries','delivery')} for x in effective_rules(s,role)]
  b+=json.dumps(md,sort_keys=True,separators=(',',':')).encode()+b'\0'
 for key in sorted(profile_specs(s)):
  b+=b'profile\0'+key.encode()+b'\0'+render_profile_with_version(s,key,VERSION_PLACEHOLDER).encode()+b'\0'
 return _h(bytes(b))
def _rule_fingerprint(x):return _h(json.dumps({k:x.get(k) for k in ('id','text','impact','surface','action_boundaries')},sort_keys=True,separators=(',',':')).encode())
def rule_fingerprints(s):return {r:{x['id']:_rule_fingerprint(x) for x in effective_rules(s,r)} for r in s['roles']}
def renderer_fingerprint():
 return _h('\0'.join((STARTUP_TEMPLATE,HANDOFF_BOUNDARY,CHATTY_BLOCK_START,CHATTY_BLOCK_END,DESIGN_BLOCK_START,DESIGN_BLOCK_END,inspect.getsource(chatty_contract),inspect.getsource(design_principles_rule),inspect.getsource(shared_rules),inspect.getsource(_design_principles_block),inspect.getsource(_render_role_index_design_principles),inspect.getsource(_render_chatty_lines),inspect.getsource(_render_project_chatty_lines),inspect.getsource(_root_chatty_block),inspect.getsource(_render_root_instructions),inspect.getsource(repository_config),inspect.getsource(context_dependencies),inspect.getsource(_render_trigger_destinations),inspect.getsource(_render_context_dependencies),inspect.getsource(render_role_with_version),inspect.getsource(profile_specs),inspect.getsource(render_profile_with_version),inspect.getsource(_render_test_candidate_kernel),inspect.getsource(render_fast_track_overlay_block),inspect.getsource(project_settings_policy),inspect.getsource(render_project_settings_payload),inspect.getsource(render_test_candidate),inspect.getsource(kernel_identity))).encode())
def _impact(c):
 x=str(c.get('impact','')).strip()
 if x not in {'compatible','additive','breaking'}: raise KernelError(f"explicit transition impact required for {c.get('rule_id','<unknown>')!r}")
 return x
def _incoming(manifest):
 c=str(manifest['canonical_version']); incoming=[x for x in manifest['change_history'] if str(x.get('to_version'))==c]
 designated=str(manifest.get('current_transition_from','')).strip()
 if designated:
  e=[x for x in incoming if str(x.get('from_version'))==designated]
  if len(e)!=1: raise KernelError('current_transition_from must identify exactly one canonical change_history edge')
  return e[0]
 if len(incoming)!=1: raise KernelError('canonical version must have exactly one incoming change_history edge unless current_transition_from is set')
 return incoming[0]
def _validate_current_edge_classification(m,s):
 e=_incoming(m); prior=e.get('from_rule_fingerprints'); roles=set(s['roles'])
 if not e.get('changes'): raise KernelError('current drift edge classification mismatch: no changes')
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
 structural_renderer=[c for c in renderer if str(c.get('rule_id'))!='renderer:chatty-contract']
 if changedr and not structural_renderer: raise KernelError('renderer changed without renderer:* classification')
 if not changedr and structural_renderer: raise KernelError('renderer classifications exist but renderer unchanged')
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
def _retirement_version(raw):
 if not isinstance(raw,dict): raise KernelError('required version retirement entries must be objects')
 version=str(raw.get('version','')).strip()
 if not version: raise KernelError('required version retirement requires exact version')
 for key in STANDING_SUPERSESSION_FIELDS:
  if not str(raw.get(key,'')).strip(): raise KernelError(f'required version retirement for {version} requires {key}')
 if str(raw.get('authority_type')) not in STANDING_SUPERSESSION_AUTHORITY_TYPES: raise KernelError(f'required version retirement for {version} has unsupported authority type')
 return version

def required_versions(m,*,active_only=True):
 inv=m.get('required_version_inventory')
 if not isinstance(inv,dict) or inv.get('schema_version')!=REQUIRED_VERSION_INVENTORY_SCHEMA: raise KernelError(f'manifest.required_version_inventory schema_version must be {REQUIRED_VERSION_INVENTORY_SCHEMA}')
 versions=inv.get('versions'); retirements=inv.get('retirements',[])
 if not isinstance(versions,list) or not versions or any(not str(x).strip() for x in versions): raise KernelError('required version inventory requires non-empty versions')
 versions=[str(x) for x in versions]
 if versions!=sorted(set(versions)): raise KernelError('required version inventory versions must be unique and sorted')
 if not isinstance(retirements,list): raise KernelError('required version inventory retirements must be a list')
 retired=[]
 for raw in retirements: retired.append(_retirement_version(raw))
 if retired!=sorted(set(retired)): raise KernelError('required version retirements must be unique and sorted by version')
 unknown=set(retired)-set(versions)
 if unknown: raise KernelError(f'required version retirements reference unknown versions: {sorted(unknown)}')
 canonical=str(m.get('canonical_version',''))
 if canonical not in versions: raise KernelError('canonical version must be present in required version inventory')
 if canonical in retired: raise KernelError('canonical version cannot be retired')
 first,pre=_legacy_floor(m)
 if first not in versions: raise KernelError('first drift-aware version must be present in required version inventory')
 overlap=set(pre)&set(versions)
 if overlap: raise KernelError(f'pre-floor versions cannot be required drift-aware versions: {sorted(overlap)}')
 return [v for v in versions if not active_only or v not in set(retired)]

def validate_required_version_topology(m):
 active=required_versions(m)
 for version in active:
  try:_change_path(m,version)
  except KernelError as e: raise KernelError(f'required version {version} is not represented/reachable to current canonical: {e}') from e
 return active

def validate_authoritative_base_preservation(base,candidate):
 base_active=set(required_versions(base)); candidate_all=set(required_versions(candidate,active_only=False)); candidate_active=set(required_versions(candidate))
 retired={_retirement_version(x) for x in candidate['required_version_inventory'].get('retirements',[])}
 missing=sorted(v for v in base_active if v not in candidate_active and v not in retired)
 deleted=sorted(v for v in base_active if v not in candidate_all and v not in retired)
 if missing or deleted: raise KernelError(f'candidate truncates authoritative required Project history: missing={missing or deleted}')
 return sorted(base_active)

def _prior_fingerprint_snapshot(s):
 return {'_shared':{x['id']:_rule_fingerprint(x) for x in shared_rules(s)},'_roles':{role:{x['id']:_rule_fingerprint(x) for x in _rules(s['roles'][role].get('rules'),f'roles.{role}.rules')} for role in s['roles']}}

def _effective_rule_maps(s):
 return {role:{x['id']:x for x in effective_rules(s,role)} for role in s['roles']}

def _transition_changes(old_s,new_s):
 if set(old_s.get('roles',{}))!=set(new_s.get('roles',{})): raise KernelError('automatic reconciliation cannot change Project role topology')
 old=_effective_rule_maps(old_s); new=_effective_rule_maps(new_s); grouped={}
 for role in sorted(new):
  for rid in sorted(set(old[role])|set(new[role])):
   a=old[role].get(rid); b=new[role].get(rid)
   if (None if a is None else _rule_fingerprint(a))==(None if b is None else _rule_fingerprint(b)): continue
   if b is None: raise KernelError(f'automatic reconciliation cannot classify rule removal {rid!r}; explicit authored transition required')
   impact=str(b.get('impact','')).strip()
   if impact=='breaking': raise KernelError(f'automatic reconciliation refuses BREAKING rule change {rid!r}; explicit proof-backed transition required')
   key=(rid,impact,str(b['surface']),tuple(b['action_boundaries']))
   grouped.setdefault(key,[]).append(role)
 changes=[]
 for (rid,impact,surface,bounds),roles in sorted(grouped.items()):
  changes.append({'rule_id':rid,'roles':roles,'impact':impact,'action_boundaries':list(bounds),'surface':surface})
 if chatty_contract(old_s)!=chatty_contract(new_s):
  changes.append({'rule_id':'renderer:chatty-contract','roles':['*'],'impact':'additive','action_boundaries':['handoff'],'surface':'presentation'})
 if not changes: raise KernelError('reconciliation source produces no canonical rule change')
 return changes

def _version_for_source(m,s): return f"{m.get('version_namespace','')}-{kernel_identity(s)[:12]}"

def _finalize_candidate_manifest(m,s):
 m['kernel_identity_sha256']=kernel_identity(s); m['canonical_version']=_version_for_source(m,s); m['source_sha256']=_semantic_json_hash(s); m['generated_sha256']=generated_sha256(m,s)
 return m

def generate_candidate_manifest(base,base_source,target_source):
 validate_change_history(base,base_source)
 target_version=_version_for_source(base,target_source)
 if target_version==str(base['canonical_version']): raise KernelError('candidate source has the same canonical Project identity as authoritative base')
 out=copy.deepcopy(base); old=str(base['canonical_version']); out['canonical_version']=target_version; out['current_transition_from']=old
 out['change_history']=copy.deepcopy(base['change_history'])+[{'from_version':old,'to_version':target_version,'changes':_transition_changes(base_source,target_source),'from_rule_fingerprints':_prior_fingerprint_snapshot(base_source),'from_renderer_fingerprint':renderer_fingerprint()}]
 inv=copy.deepcopy(base['required_version_inventory']); inv['versions']=sorted(set(map(str,inv['versions']))|{target_version}); out['required_version_inventory']=inv
 _finalize_candidate_manifest(out,target_source); validate_change_history(out,target_source); validate_authoritative_base_preservation(base,out)
 return out

def _fingerprint_in_history(m,role,rid,fingerprint):
 for edge in m.get('change_history',[]):
  prior=edge.get('from_rule_fingerprints',{}) if isinstance(edge,dict) else {}
  if not isinstance(prior,dict): continue
  shared=prior.get('_shared',{}); roles=prior.get('_roles',{})
  if isinstance(shared,dict) and shared.get(rid)==fingerprint:return True
  if isinstance(roles,dict) and isinstance(roles.get(role),dict) and roles[role].get(rid)==fingerprint:return True
 return False

def _validate_reconciled_source(base,base_source,candidate,candidate_source,merged_source):
 if set(base_source.get('roles',{}))!=set(candidate_source.get('roles',{})) or set(base_source.get('roles',{}))!=set(merged_source.get('roles',{})): raise KernelError('reconciliation role topology mismatch')
 bm,cm,mm=_effective_rule_maps(base_source),_effective_rule_maps(candidate_source),_effective_rule_maps(merged_source)
 for role in sorted(bm):
  for rid in sorted(set(bm[role])|set(cm[role])):
   b=bm[role].get(rid); c=cm[role].get(rid); z=mm[role].get(rid)
   bf=None if b is None else _rule_fingerprint(b); cf=None if c is None else _rule_fingerprint(c); zf=None if z is None else _rule_fingerprint(z)
   if bf==cf:
    if zf!=bf: raise KernelError(f'reconciled source changes unchanged rule {role}/{rid}')
    continue
   if bf is None:
    if zf!=cf: raise KernelError(f'reconciled source drops candidate additive rule {role}/{rid}')
    continue
   if cf is None:
    if zf!=bf: raise KernelError(f'reconciled source drops authoritative base rule {role}/{rid}')
    continue
   candidate_is_ancestor=_fingerprint_in_history(base,role,rid,cf); base_is_ancestor=_fingerprint_in_history(candidate,role,rid,bf)
   if candidate_is_ancestor and not base_is_ancestor:
    if zf!=bf: raise KernelError(f'reconciled source revives stale rule {role}/{rid}')
   elif base_is_ancestor and not candidate_is_ancestor:
    if zf!=cf: raise KernelError(f'reconciled source drops newer candidate rule {role}/{rid}')
   else: raise KernelError(f'ambiguous/incompatible concurrent rule history for {role}/{rid}')
 return True

def _merge_required_inventories(base,candidate,canonical):
 # The authoritative main inventory is the published set. A concurrent candidate's own
 # branch-only canonical is represented in convergence topology but is not promoted to
 # published-required merely because it participated in reconciliation.
 versions=sorted(set(required_versions(base,active_only=False))|{canonical}); by={}
 for raw in list(base['required_version_inventory'].get('retirements',[]))+list(candidate['required_version_inventory'].get('retirements',[])):
  version=_retirement_version(raw)
  if version not in versions: continue
  prior=by.get(version)
  if prior is not None and prior!=raw: raise KernelError(f'conflicting retirement authority for {version}')
  by[version]=copy.deepcopy(raw)
 return {'schema_version':REQUIRED_VERSION_INVENTORY_SCHEMA,'versions':versions,'retirements':[by[k] for k in sorted(by)]}

def reconcile_manifests(base,base_source,candidate,candidate_source,merged_source):
 validate_change_history(base,base_source); validate_change_history(candidate,candidate_source)
 _validate_reconciled_source(base,base_source,candidate,candidate_source,merged_source)
 new_version=_version_for_source(base,merged_source); out=copy.deepcopy(base); old_base=str(base['canonical_version']); old_candidate=str(candidate['canonical_version'])
 if new_version==old_base:
  if old_candidate!=old_base:
   out['change_history']=copy.deepcopy(base['change_history'])+[{'from_version':old_candidate,'to_version':old_base,'changes':_transition_changes(candidate_source,merged_source),'from_rule_fingerprints':_prior_fingerprint_snapshot(candidate_source),'from_renderer_fingerprint':renderer_fingerprint()}]
  out['required_version_inventory']=_merge_required_inventories(base,candidate,new_version); _finalize_candidate_manifest(out,merged_source); validate_change_history(out,merged_source); validate_authoritative_base_preservation(base,out); return out
 edges=copy.deepcopy(base['change_history']); by={str(e['from_version']):e for e in edges}
 for edge in sorted(candidate.get('change_history',[]),key=lambda x:(str(x.get('from_version')),str(x.get('to_version')))):
  a=str(edge.get('from_version')); existing=by.get(a)
  if existing is None: edges.append(copy.deepcopy(edge)); by[a]=edge
  elif existing==edge: continue
  # Authoritative base successor wins at a fork. The candidate canonical is preserved below as a convergence predecessor.
 base_edge={'from_version':old_base,'to_version':new_version,'changes':_transition_changes(base_source,merged_source),'from_rule_fingerprints':_prior_fingerprint_snapshot(base_source),'from_renderer_fingerprint':renderer_fingerprint()}
 edges.append(base_edge)
 if old_candidate not in {old_base,new_version}:
  if old_candidate in {str(e.get('from_version')) for e in edges}: raise KernelError(f'ambiguous candidate canonical successor for {old_candidate}')
  edges.append({'from_version':old_candidate,'to_version':new_version,'changes':_transition_changes(candidate_source,merged_source),'from_rule_fingerprints':_prior_fingerprint_snapshot(candidate_source),'from_renderer_fingerprint':renderer_fingerprint()})
 out['change_history']=edges; out['canonical_version']=new_version; out['current_transition_from']=old_base; out['required_version_inventory']=_merge_required_inventories(base,candidate,new_version)
 _finalize_candidate_manifest(out,merged_source); validate_change_history(out,merged_source); validate_authoritative_base_preservation(base,out)
 return out

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
 seen=set(); versions=set()
 for e in h:
  if not isinstance(e,dict): raise KernelError('change_history entries must be objects')
  a=str(e.get('from_version','')).strip(); b=str(e.get('to_version','')).strip(); ch=e.get('changes')
  if not a or not b or a==b or a in seen: raise KernelError(f'ambiguous/invalid change_history transition from {a!r}')
  seen.add(a); versions.update((a,b))
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
 _validate_current_edge_classification(m,s); validate_required_version_topology(m)
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
 expected_semantic={'admission_order':list(REPOSITORY_CONTEXT_ADMISSION_ORDER),'tiny_targeted_reads_exempt':True,'reentry_events':['fresh-or-replacement-session','post-compaction-reground','affected-role-switch','main-movement-with-absent-or-stale-witness'],'failure_scope':'affected-substantial-conclusion-only','bundle_authority':'read-only-context','current_state_authorities':['GitHub','Asana'],'ordinary_chatgpt_pr_review':{'bundle_unavailable':'connector-native-exact-evidence-fallback','bundle_used':'exact-current-validation-required'}}
 if semantic!=expected_semantic: raise KernelError('standing invariant repository-context-admission semantic contract changed without supersession')
 coverage=entry.get('coverage')
 if not isinstance(coverage,dict): raise KernelError('standing invariant repository-context-admission requires coverage')
 if coverage.get('source_rule_id')!='repository-context-admission' or coverage.get('source_rule_fingerprint')!=REPOSITORY_CONTEXT_SOURCE_RULE_FINGERPRINT or set(coverage.get('required_eval_ids',[]))!=set(REPOSITORY_CONTEXT_EVAL_IDS) or set(coverage.get('rendered_roles',[]))!=set(REPOSITORY_CONTEXT_ROLES) or coverage.get('completion_role')!='integration' or coverage.get('completion_rule_id')!='integration-standing-policy-readback' or coverage.get('completion_rule_fingerprint')!=REPOSITORY_CONTEXT_COMPLETION_RULE_FINGERPRINT: raise KernelError('standing invariant repository-context-admission coverage weakened without supersession')
 shared={x['id']:x for x in shared_rules(s)}
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
 for key in sorted(profile_specs(s)): parts.append('profile:'+key+'\0'+render_profile_with_version(s,key,str(m['canonical_version'])))
 return _h('\0'.join(parts).encode())
def load_canonical(*,validate_history=True):
 m=_read_manifest(MANIFEST_PATH); p=PROJECT_DIR/str(m.get('source_file','')); s=_read_json(p)
 project_settings_policy(m)
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
def generated_profile_paths(m,s):
 profiles=profile_specs(s); files=m.get('generated_profile_files',{})
 if not isinstance(files,dict) or set(files)!=set(profiles): raise KernelError('generated profile file map mismatch')
 return {k:PROJECT_DIR/str(files[k]) for k in profiles}
def render_all(*,check):
 m,s=load_canonical(); out=[]; _render_root_instructions(s,check=check); _render_claude_operator_style(s,check=check); _render_role_index_design_principles(s,check=check)
 overlay=project_settings_compatibility_overlay(); fixture={'candidate_version':'dish-chatgpt-projects-test-g1','pr_number':1,'candidate_ref':'refs/pull/1/head','candidate_head':'a'*40,'candidate_manifest_sha256':'b'*64,'production_version':m['canonical_version']}
 for r,p in generated_paths(m,s).items():
  production=render_project_settings_payload(m,s,r); text=production['text']; n=production['total_chars']
  render_project_settings_payload(m,s,r,overlay=overlay)
  render_project_settings_payload(m,s,r,channel='test',**fixture)
  render_project_settings_payload(m,s,r,channel='test',overlay=overlay,**fixture)
  if check:
   if not p.is_file() or p.read_text()!=text: raise KernelError(f'generated kernel differs: {p}')
  else:p.write_text(text)
  out.append((r,n))
 for key,p in generated_profile_paths(m,s).items():
  text=render_profile_with_version(s,key,str(m['canonical_version'])); n=len(text); profile_limit=profile_specs(s)[key]['max_chars']
  if n>profile_limit: raise KernelError(f'profile {key} exceeds {profile_limit} chars: {n}')
  if check:
   if not p.is_file() or p.read_text()!=text: raise KernelError(f'generated profile differs: {p}')
  else:p.write_text(text)
  out.append((f'profile:{key}',n))
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

REQUIRED_EVAL_IDS={'action-first-lifecycle-output', 'active-gate-blocker-cannot-be-deferred', 'additive-evidence-drift', 'allowed-specialist-implementation-composition', 'audit-dedupe-existing-finding', 'audit-exact-baseline', 'audit-missing-authority-fails-closed', 'audit-moved-baseline-current-blocker', 'audit-new-finding-backlog-only', 'audit-refuses-mutation-authority', 'audit-specialist-context-no-authority', 'authenticated-account-not-human-decision', 'chat-only-review-verdict-not-complete', 'chatty-authorized-action-before-narration', 'chatty-high-level-review-summary', 'chatty-progress-is-not-completion', 'chatty-session-correction-latches', 'chatty-status-reconciles-before-reroute', 'code-smell-dedupe-log-and-continue', 'code-smell-true-blocker-stays-active', 'comparison-incompatible-target-escalates-implementation', 'compatible-concise-output-drift', 'compatible-wording-drift', 'configured-repository-pr-routing', 'coordinator-check-everything-mixed-state', 'coordinator-pr-intake-automatic-review', 'cross-role-context-bleed', 'current-template-lookup', 'development-workflow-context-preload-no-authority', 'development-workflow-pr40-fallback-context', 'development-workflow-pr60-test-scope-context', 'disposable-fixture-still-needs-health', 'durable-review-classification', 'durable-review-classification-development-workflow', 'chat-only-review-verdict-not-complete-development-workflow', 'failed-ci-ownership-before-fix', 'five-whys-evidence-discipline', 'five-whys-reground-reload', 'forbidden-implicit-role-expansion', 'friction-active-blocker-routes-to-active-work', 'friction-dedupe-no-urgency', 'handoff-conflicts-with-role-authority', 'implementation-escalation-is-action-first', 'implementation-rejects-patch-only-completion', 'integration-bounded-reconciliation', 'integration-breaking-merge-drift', 'integration-rejects-head-mismatch', 'live-authority-over-stale-memory', 'no-valid-fallback', 'post-merge-asana-residual-gate', 'project-drift-current-silent', 'project-drift-integrity-error', 'project-drift-pre-d96-legacy', 'project-drift-self-compatible', 'project-drift-v708-review-compatible', 'publication-blocker-forbids-unsafe-shortcuts', 'publication-local-candidate-transport-honesty', 'publication-completion-invalidates-prior-review', 'publication-fully-published-local-certification', 'publication-handoff-before-human-notification', 'publication-materializer-eligible-blocker', 'publication-unsafe-governed-path-blocker', 'repository-context-admission-consequential-reasoning', 'repository-context-admission-missing-bundle', 'repository-context-admission-reentry', 'repository-context-admission-stale-main', 'repository-context-admission-tiny-lookup', 'repository-friction-discovery', 'review-breaking-completion-drift', 'review-exact-head-completion', 'reviewed-head-movement-classification', 'scope-amplification-checkpoint', 'separate-pr-does-not-clear-independent-blocker', 'shared-resource-concurrency-preflight', 'skipped-version-breaking-drift', 'skipped-version-nonbreaking-drift', 'stale-project-version', 'standing-policy-post-integration-main-readback', 'supported-operation-stays-local-system-access', 'task-history-before-no-op', 'unrelated-role-drift', 'valid-action-fallback', 'design-principles-harmless-overlap', 'design-principles-no-invented-manual-gate', 'external-defect-continue-original', 'external-defect-required-owner-lineage', 'truthful-liveness-attempt-isolation', 'worker-role-phase-activation-boundary', 'review-bundle-unavailable-proceeds', 'review-real-evidence-boundary-routes-local', 'review-stale-bundle-rejected', 'review-bundle-outage-regression-fixtures'}
REQUIRED_EVAL_IDS|={'manual-worker-block-switches-without-second-marco-prompt','manual-worker-fix-publishes-and-stops-before-self-review','manual-worker-missing-automated-bookkeeping-is-not-a-review-gate'}
REQUIRED_EVAL_IDS|={'development-workflow-asana-legacy-mode','development-workflow-asana-v2-mode','development-workflow-asana-v3-abort','development-workflow-asana-contradiction-abort','development-workflow-asana-unknown-version-abort'}
REQUIRED_EVAL_IDS|={'development-workflow-asana-later-hold-controls','development-workflow-asana-audit-blocker-no-inference','development-workflow-asana-design-awaits-agentic-review','development-workflow-asana-later-prohibition-controls','development-workflow-asana-folded-owner-done','development-workflow-asana-post-merge-rollout','development-workflow-asana-named-dependency','development-workflow-asana-raw-intake','development-workflow-asana-ambiguous-chronology','development-workflow-asana-projection-contradiction','development-workflow-asana-comment-is-not-state','development-workflow-asana-contextual-version','development-workflow-asana-priority-absent','development-workflow-asana-historical-final-stamp','development-workflow-asana-stale-session-fresh-read'}
REQUIRED_EVAL_IDS|={'asana-v2-project-registry-postgresql-contradictory-sections','asana-v2-project-registry-coordinator-legacy-bare-name','asana-v2-project-registry-unknown-version-stop-and-flag','asana-v2-project-registry-unregistered-project-refusal'}
REQUIRED_EVAL_IDS|={'review-v3-stale-design-verdict-does-not-project-current', 'review-v3-bounded-recovery-polling-allowed', 'review-v3-event-driven-polling-intent-drift', 'review-v3-headline-exact-approval-sticky', 'review-v3-task-changed-after-dispatch', 'review-v3-development-workflow-design-review-capability', 'review-v3-review-focus-open-ended', 'review-v3-implementation-consumes-handoff-as-projection', 'review-v3-signed-intent-deviation', 'review-v3-complexity-overshoot-challenge', 'review-v3-universal-quantifier-not-enumeration', 'review-v3-wrong-spec-green-tests-block', 'review-v3-handoff-pre-dispatch-fidelity', 'review-v3-learned-risk-applicability', 'review-v3-process-defect-preserves-semantic-finding', 'review-v3-protected-invariant-missing-detected', 'review-v3-handoff-drift-block', 'review-v3-compatibility-unknown-not-safe-remove', 'review-v3-headline-agent-inference-rejected', 'review-v3-false-operational-readiness'}
REQUIRED_EVAL_IDS|={'review-v3-audit-excluded', 'review-v3-stale-generation-does-not-transfer', 'review-v3-coordinator-design-review', 'review-v3-self-authored-design-fails-closed', 'review-v3-ambiguous-review-type', 'review-v3-development-workflow-design-review', 'review-v3-development-workflow-code-review-route', 'review-v3-intent-invariants-and-focus'}
REQUIRED_EVAL_IDS|={'review-v3-operator-workflow-before-after','review-v3-stale-architecture-block','review-v3-frozen-generation-zero-mutation','review-v3-epistemic-stop-prevents-false-pass','review-v3-epistemic-stop-prevents-false-block','review-v3-rollout-misses-failure','review-v3-wrong-asana-context','review-v3-cross-host-reground-loop','review-v3-parallel-lineage-reuse-race','review-v3-stable-base-conflict-cost','review-v3-rollback-scope-mismatch','review-v3-competing-intent-summary','review-v3-unsupported-external-inference','review-v3-dangerous-ci-ownership','review-v3-protected-invariant-violation','review-v3-stale-section-write-converges','review-v3-headline-paraphrase-requires-reapproval','review-v3-headline-evidence-unrecoverable'}
REQUIRED_EVAL_IDS|={'review-correction-r3-block-code-fix','review-correction-r3-merge-no-fix','review-correction-r3-design-task-shape-route','review-correction-r3-batch-isolation'}
REQUIRED_EVAL_IDS|={'autonomy-new-conversational-implementation-needs-one-confirmation','autonomy-active-implementation-does-not-reconfirm','autonomy-worker-formal-block-fix-no-second-confirmation','autonomy-review-correction-r3-task-shape-implementation-no-second-confirmation','autonomy-known-creator-needs-independent-pass','autonomy-manual-worker-missing-automated-provenance-is-not-blocker','autonomy-ambiguous-authorship-without-pass-routes-review','autonomy-transition-evidence-does-not-fabricate-lifecycle-state'}
REQUIRED_EVAL_IDS|={'review-v4-author-falsification-before-handoff','review-v4-design-author-self-pass-not-independent','review-v4-complete-requirements-no-last-writer-wins','review-v4-implementation-readiness-no-consequential-invention','review-v4-real-event-producer-required','review-v4-verbatim-marco-wording','review-v4-verbatim-marco-wording-development-workflow','review-v4-coordinator-final-admission-delta','review-v4-needs-human-review-surfaces-current-revision','review-v4-task-go-remains-autonomous','review-v4-block-fix-fresh-independent-review','review-v4-followup-capture-does-not-block','review-v4-code-smells-capture-outage-nonblocking','review-v4-ship-safe-polish-followup','review-v4-dp11-targeted-domain-authority'}
REQUIRED_EVAL_IDS|={'scope-guardrail-design-proportionality-signoff','scope-guardrail-design-remedy-nonauthoritative','scope-guardrail-code-defect-within-design','scope-guardrail-code-review-new-design-requirement','scope-guardrail-fix-agent-obeys-classified-blocker','scope-guardrail-focused-rereview','scope-guardrail-non-pilot-preserves-standing-behavior'}
REQUIRED_EVAL_IDS|={'durable-before-terminal-design-review-verdict','durable-before-terminal-projection-required','durable-before-terminal-stale-generation-race','durable-before-terminal-fallback-available','durable-before-terminal-all-routes-exhausted','durable-before-terminal-continue-actionable-work','durable-before-terminal-separate-worker-running','durable-before-terminal-stronger-clearance-evidence'}
ATTENTION_EVAL_IDS={'attention-depth-is-session-persistent','attention-minimum-packet-survives-50-percent','attention-progressive-disclosure-at-200-percent','attention-recovery-interaction'}
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
 shared={x['id']:x for x in shared_rules(s)}
 for pid,e in by.items():
  canonical=_dependency_path(e.get('canonical_document'),f'preservation.{pid}.canonical_document')
  index_document=_dependency_path(e.get('index_document'),f'preservation.{pid}.index_document')
  link=str(e.get('index_link','')).strip(); index_text=(REPO_ROOT/index_document).read_text()
  if not link or link not in index_text: raise KernelError(f'preservation {pid} index link missing from {index_document}')
  rid=str(e.get('shared_rule_id','')).strip()
  if rid!=pid or rid not in shared: raise KernelError(f'preservation {pid} shared rule missing from canonical source')
  if Path(canonical).name not in shared[rid]['text']: raise KernelError(f'preservation {pid} shared rule does not point to {canonical}')
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
 required=REQUIRED_EVAL_IDS|ATTENTION_EVAL_IDS
 if seen!=required: raise KernelError(f'eval set mismatch missing={sorted(required-seen)} extras={sorted(seen-required)}')
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
def _load_manifest_source_files(manifest_path:Path,source_path:Path|None=None):
 m=_read_manifest(manifest_path); p=source_path or manifest_path.parent/str(m.get('source_file','')); s=_read_json(p)
 if m.get('source_sha256')!=_semantic_json_hash(s): raise KernelError(f'manifest/source semantic hash mismatch: {manifest_path}')
 if s.get('schema_version')!=m.get('schema_version'): raise KernelError(f'manifest/source schema mismatch: {manifest_path}')
 kid=kernel_identity(s); expected=f"{m.get('version_namespace','')}-{kid[:12]}"
 if m.get('kernel_identity_sha256')!=kid or m.get('canonical_version')!=expected: raise KernelError(f'manifest/source canonical identity mismatch: {manifest_path}')
 if m.get('generated_sha256')!=generated_sha256(m,s): raise KernelError(f'manifest/source generated digest mismatch: {manifest_path}')
 validate_change_history(m,s); return m,s

def command_admit(base_manifest:Path,candidate_manifest:Path,base_source:Path|None=None,candidate_source:Path|None=None):
 base,bs=_load_manifest_source_files(base_manifest,base_source); candidate,cs=_load_manifest_source_files(candidate_manifest,candidate_source)
 validate_authoritative_base_preservation(base,candidate); validate_required_version_topology(candidate)
 print(f"PASS authoritative-base-preservation base={base['canonical_version']} candidate={candidate['canonical_version']}")

def command_reconcile(base_manifest:Path,source:Path,output:Path,base_source:Path|None=None,candidate_manifest:Path|None=None,candidate_source:Path|None=None):
 base,bs=_load_manifest_source_files(base_manifest,base_source); target=_read_json(source)
 if candidate_manifest is None:
  if candidate_source is not None: raise KernelError('--candidate-source requires --candidate-manifest')
  out=generate_candidate_manifest(base,bs,target)
 else:
  candidate,cs=_load_manifest_source_files(candidate_manifest,candidate_source); out=reconcile_manifests(base,bs,candidate,cs,target)
 _write_manifest(output,out); print(f"WROTE {output} canonical_version={out['canonical_version']}")
def command_refresh(base_manifest:Path,source:Path,base_source:Path|None=None,candidate_manifest:Path|None=None,candidate_source:Path|None=None):
 if source.resolve()!=PROJECT_DIR.joinpath('source.json').resolve(): raise KernelError('refresh requires the canonical docs/chatgpt-projects/source.json')
 command_reconcile(base_manifest,source,MANIFEST_PATH,base_source,candidate_manifest,candidate_source)
 render_all(check=False)
 command_check()
 m,s=load_canonical()
 print('PASTE-READY PROJECT KERNELS')
 for role,path in sorted(generated_paths(m,s).items()): print(f'{role}: {path}')
def _write_json(p,v):p.write_text(json.dumps(v,indent=2,sort_keys=True)+'\n')
def _write_manifest(p,v):
 out=copy.deepcopy(v); history=out.pop('change_history',None)
 if not isinstance(history,list):raise KernelError('manifest.change_history must be a list')
 shard_dir=p.with_name(p.stem+'-history'); shard_dir.mkdir(parents=True,exist_ok=True)
 chunks=[]; chunk=[]; size=0
 for edge in history:
  edge_size=len(json.dumps(edge,indent=2,sort_keys=True).encode())
  if chunk and size+edge_size>180000:chunks.append(chunk); chunk=[]; size=0
  chunk.append(edge); size+=edge_size
 if chunk:chunks.append(chunk)
 names=[]
 for index,chunk in enumerate(chunks):
  shard=shard_dir/f'{index:02d}.json'; _write_json(shard,chunk); names.append(shard.relative_to(p.parent).as_posix())
 out['change_history_shards']=names; _write_json(p,out)
def _parser():
 p=argparse.ArgumentParser(description=__doc__); s=p.add_subparsers(dest='command',required=True); r=s.add_parser('render'); r.add_argument('--check',action='store_true'); s.add_parser('check'); pe=s.add_parser('prepare-eval'); pe.add_argument('--output',required=True,type=Path); ev=s.add_parser('eval'); g=ev.add_mutually_exclusive_group(required=True); g.add_argument('--results',type=Path); g.add_argument('--runner-command'); ev.add_argument('--save-results',type=Path); v=s.add_parser('version'); v.add_argument('--project-version',required=True); v.add_argument('--role',required=True); v.add_argument('--action-boundary',default='role-critical-write')
 ad=s.add_parser('admit'); ad.add_argument('--base-manifest',required=True,type=Path); ad.add_argument('--base-source',type=Path); ad.add_argument('--candidate-manifest',required=True,type=Path); ad.add_argument('--candidate-source',type=Path)
 rc=s.add_parser('reconcile'); rc.add_argument('--base-manifest',required=True,type=Path); rc.add_argument('--base-source',type=Path); rc.add_argument('--source',required=True,type=Path); rc.add_argument('--candidate-manifest',type=Path); rc.add_argument('--candidate-source',type=Path); rc.add_argument('--output',required=True,type=Path)
 rf=s.add_parser('refresh'); rf.add_argument('--base-manifest',required=True,type=Path); rf.add_argument('--base-source',type=Path); rf.add_argument('--source',required=True,type=Path); rf.add_argument('--candidate-manifest',type=Path); rf.add_argument('--candidate-source',type=Path)
 return p
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
  elif a.command=='admit': command_admit(a.base_manifest,a.candidate_manifest,a.base_source,a.candidate_source)
  elif a.command=='reconcile': command_reconcile(a.base_manifest,a.source,a.output,a.base_source,a.candidate_manifest,a.candidate_source)
  elif a.command=='refresh': command_refresh(a.base_manifest,a.source,a.base_source,a.candidate_manifest,a.candidate_source)
  else:
   ok,msg=version_status(a.project_version,a.role,a.action_boundary); print(msg); return 0 if ok else 3
 except KernelError as e: print(f'ERROR: {e}',file=sys.stderr); return 2
 return 0
if __name__=='__main__': raise SystemExit(main())
