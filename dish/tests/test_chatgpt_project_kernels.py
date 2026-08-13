from __future__ import annotations
import copy, importlib.util, json
from pathlib import Path
import pytest
DISH_ROOT=Path(__file__).resolve().parents[1]; SCRIPT=DISH_ROOT/'scripts'/'chatgpt_project_kernels.py'
SPEC=importlib.util.spec_from_file_location('chatgpt_project_kernels',SCRIPT); assert SPEC and SPEC.loader
kernels=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(kernels)

def _scenario(sid): return next(x for x in kernels._evals() if x['id']==sid)
def _result(payload,cid): return next(x for x in payload['results'] if x['case_id']==cid)
def _obs(sid,role):
 if sid=='review-exact-head-completion': return [
  {'seq':1,'kind':'durable_write','operation':'pull_request_review','method':'COMMENT','pr':41,'head_sha':'H','write_id':'r41'},
  {'seq':2,'kind':'readback','operation':'pull_request_review','method':'COMMENT','pr':41,'head_sha':'H','write_id':'r41','verified':True}]
 if sid=='valid-action-fallback': return [
  {'seq':1,'kind':'capability_discovery','operation':'pull_request_review','pr':42,'available_methods':['COMMENT'],'unavailable_methods':['APPROVE']},
  {'seq':2,'kind':'durable_write','operation':'pull_request_review','method':'COMMENT','pr':42,'head_sha':'abc123','write_id':'r42'},
  {'seq':3,'kind':'readback','operation':'pull_request_review','method':'COMMENT','pr':42,'head_sha':'abc123','write_id':'r42','verified':True}]
 if sid=='publication-handoff-before-human-notification': return [
  {'seq':1,'kind':'durable_write','operation':'publication_blocker_handoff','pr':52,'state':'LOCAL IMPLEMENTATION COMPLETION REQUIRED','handoff_complete':True,'write_id':'h52'},
  {'seq':2,'kind':'readback','operation':'publication_blocker_handoff','pr':52,'state':'LOCAL IMPLEMENTATION COMPLETION REQUIRED','write_id':'h52','verified':True},
  {'seq':3,'kind':'human_notification','operation':'control_plane_message','pr':52,'action':'local_implementation_completion','details_location':'pull_request'}]
 if sid=='configured-repository-pr-routing':
  return [{'seq':1,'kind':'connector_read','operation':'pull_request_read','connector':'GitHub','repository':'marcogallotta/ai-tools','pr':31 if role=='review' else 34}]
 return []
def _passing():
 m,_=kernels.load_canonical(); results=[]
 for s in kernels._evals():
  for role in s['roles']:
   results.append({'case_id':f"{s['id']}::{role}",'fresh_chat_id':f"chat-{s['id']}-{role}",'assistant_response':{'outcome':s['expected_outcome'],'actions':list(s['required_actions'])},'runner_observations':_obs(s['id'],role)})
 return {'schema_version':2,'runner_protocol':'dish-chatgpt-project-behavior-v2','canonical_version':m['canonical_version'],'results':results}
def _change(rule,role,impact,boundary,surface): return {'rule_id':rule,'roles':[role],'impact':impact,'action_boundaries':[boundary],'surface':surface}
def _manifest(current,edges): return {'canonical_version':current,'change_history':edges}

def test_manifest_source_identity_topology_and_metadata():
 m,s=kernels.load_canonical(); kernels.validate_topology(s)
 assert m['canonical_version'].endswith(m['kernel_identity_sha256'][:12])
 assert kernels.kernel_identity(s)==m['kernel_identity_sha256']
 assert kernels.repository_config(s)==('marcogallotta/ai-tools','main','connected GitHub connector')
 for role in s['roles']:
  for r in kernels.effective_rules(s,role):
   assert r['impact'] in {'breaking','additive','compatible'} and r['surface'] and r['action_boundaries']

def test_missing_repository_bootstrap_fails_closed():
 _,s=kernels.load_canonical()
 for field in ('repository_full_name','default_branch','github_transport'):
  bad=copy.deepcopy(s); bad.pop(field)
  with pytest.raises(kernels.KernelError,match=field): kernels.kernel_identity(bad)

def test_current_edge_requires_exact_rule_classification():
 m,s=kernels.load_canonical(); bad=copy.deepcopy(m); edge=next(x for x in bad['change_history'] if x['to_version']==bad['canonical_version'])
 edge['changes']=[x for x in edge['changes'] if x['rule_id']!='review-action-handoff']
 with pytest.raises(kernels.KernelError,match='classification mismatch'): kernels._validate_current_edge_classification(bad,s)

def test_generated_kernels_current_bound_and_within_budget():
 m,s=kernels.load_canonical(); results=kernels.render_all(check=True); assert len(results)==7
 for role,p in kernels.generated_paths(m,s).items():
  text=p.read_text(); assert len(text)<=m['max_kernel_chars']; assert f"PROJECT_CANONICAL_VERSION: {m['canonical_version']}" in text
  assert text.index('PROJECT_REPOSITORY: marcogallotta/ai-tools')<text.index('Startup:')
  assert 'Version mismatch triggers manifest `change_history`' in text

def test_eval_contract_matrix_and_oracle_free_prepared_cases():
 ids=kernels.validate_eval_contracts(); assert set(ids)==kernels.REQUIRED_EVAL_IDS
 b=kernels.prepare_eval_bundle(); assert len(b['cases'])==34
 assert all(kernels.ORACLE_FIELDS.isdisjoint(c) for c in b['cases'])
 by={c['case_id']:c for c in b['cases']}; assert by['configured-repository-pr-routing::review']['prompt']=='review PR31'; assert by['configured-repository-pr-routing::integration']['prompt']=='merge PR34'

def test_behavior_evaluator_accepts_complete_matrix(): assert len(kernels.evaluate_behavior_results(_passing()))==34

def test_repository_routing_requires_observed_configured_connector_read():
 p=_passing(); _result(p,'configured-repository-pr-routing::review')['runner_observations']=[]
 with pytest.raises(kernels.KernelError,match='missing runner-observed evidence'): kernels.evaluate_behavior_results(p)

@pytest.mark.parametrize('operation',['global_pr_search','web_search_repository'])
def test_runner_observed_forbidden_discovery_is_rejected(operation):
 oracle={'required_observations':[{'kind':'connector_read','operation':'pull_request_read','equals':{'connector':'GitHub','repository':'marcogallotta/ai-tools','pr':31}}],'forbidden_actions':{'ask_owner_repo','global_pr_search','web_search_repository'},'require_ordered_observations':False,'observation_link_field':''}
 obs=[{'seq':1,'kind':'external_discovery','operation':operation},{'seq':2,'kind':'connector_read','operation':'pull_request_read','connector':'GitHub','repository':'marcogallotta/ai-tools','pr':31}]
 with pytest.raises(kernels.KernelError,match='runner observed forbidden operation'): kernels._validate_observed_evidence('configured-repository-pr-routing::review',oracle,obs)

def test_formal_review_needs_comment_write_and_matching_readback():
 p=_passing(); r=_result(p,'valid-action-fallback::review'); r['runner_observations'][1]['method']='APPROVE'
 with pytest.raises(kernels.KernelError,match='missing required runner observation'): kernels.evaluate_behavior_results(p)
 p=_passing(); r=_result(p,'review-exact-head-completion::review'); r['runner_observations'][1]['write_id']='other'
 with pytest.raises(kernels.KernelError,match='do not share write_id'): kernels.evaluate_behavior_results(p)

def test_fresh_chat_ids_cannot_be_reused():
 p=_passing(); p['results'][1]['fresh_chat_id']=p['results'][0]['fresh_chat_id']
 with pytest.raises(kernels.KernelError,match='reused fresh_chat_id'): kernels.evaluate_behavior_results(p)

def test_breaking_drift_blocks_only_affected_role_and_boundary():
 _,s=kernels.load_canonical(); m=_manifest('v2',[{'from_version':'v1','to_version':'v2','changes':[_change('review-formal-comment','review','breaking','review-write','lifecycle')]}])
 assert kernels.classify_project_drift('v1','review','review-write',manifest=m,source=s)['block'] is True
 assert kernels.classify_project_drift('v1','review','handoff',manifest=m,source=s)['block'] is False
 assert kernels.classify_project_drift('v1','implementation','role-critical-write',manifest=m,source=s)['impact']=='unrelated'

def test_additive_compatible_and_unrelated_drift_continue():
 _,s=kernels.load_canonical(); m=_manifest('v2',[{'from_version':'v1','to_version':'v2','changes':[_change('fallback-surface','implementation','additive','handoff','evidence'),_change('review-action-handoff','review','compatible','handoff','presentation')]}])
 d=kernels.classify_project_drift('v1','implementation','role-critical-write',manifest=m,source=s); assert not d['block'] and d['impact']=='additive'
 d=kernels.classify_project_drift('v1','review','handoff',manifest=m,source=s); assert not d['block'] and d['impact']=='compatible'
 d=kernels.classify_project_drift('v1','workflow','role-critical-write',manifest=m,source=s); assert not d['block'] and d['impact']=='unrelated'

def test_skipped_versions_fold_and_highest_relevant_impact_wins():
 _,s=kernels.load_canonical(); m=_manifest('v3',[{'from_version':'v1','to_version':'v2','changes':[_change('fallback-surface','integration','additive','merge','evidence')]},{'from_version':'v2','to_version':'v3','changes':[_change('integration-reviewed-head','integration','breaking','merge','safety')]}])
 d=kernels.classify_project_drift('v1','integration','merge',manifest=m,source=s); assert d['block'] and d['impact']=='breaking' and len(d['changes'])==2

def test_missing_version_lineage_fails_closed():
 _,s=kernels.load_canonical(); m=_manifest('v3',[{'from_version':'v2','to_version':'v3','changes':[_change('fallback-surface','implementation','additive','handoff','evidence')]}])
 with pytest.raises(kernels.KernelError,match='does not cover'): kernels.classify_project_drift('v1','implementation','role-critical-write',manifest=m,source=s)

def test_unknown_unclassified_changes_fail_closed_by_surface():
 assert kernels._impact({'surface':'authority'})=='breaking'; assert kernels._impact({'surface':'safety'})=='breaking'; assert kernels._impact({'surface':'presentation'})=='compatible'

def test_task_required_drift_eval_cases_are_present_and_scoped():
 by={s['id']:s for s in kernels._evals()}
 assert by['compatible-wording-drift']['roles']==['implementation']; assert by['unrelated-role-drift']['roles']==['implementation']
 assert by['review-breaking-completion-drift']['roles']==['review']; assert by['integration-breaking-merge-drift']['roles']==['integration']
 assert 'fold_all_skipped_versions' in by['skipped-version-breaking-drift']['required_actions']

def test_role_and_publication_boundaries_remain_high_salience():
 _,s=kernels.load_canonical(); impl=' '.join(x['text'] for x in kernels.effective_rules(s,'implementation')); rev=' '.join(x['text'] for x in kernels.effective_rules(s,'review')); integ=' '.join(x['text'] for x in kernels.effective_rules(s,'integration'))
 assert 'owned branch + commit + PR + exact head' in impl and 'Do not self-review/integrate' in impl
 assert 'formal GitHub `COMMENT` verdict' in rev and 'Review does not implement fixes' in rev
 assert 'current head must equal the exact reviewed/certified head' in integ
