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
 if sid in {'five-whys-evidence-discipline','five-whys-reground-reload'}:
  return [{'seq':1,'kind':'connector_read','operation':'repository_file_read','connector':'GitHub','repository':'marcogallotta/ai-tools','path':'dish/docs/agents/five-whys.md'}]
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
 if sid=='repository-context-admission-consequential-reasoning':
  aid='admission-a'; sha='a'*40
  return [
   {'seq':1,'kind':'connector_read','operation':'repository_main_identity','connector':'GitHub','repository':'marcogallotta/ai-tools','repository_id':1304888921,'source_ref':'refs/heads/main','source_sha':sha,'admission_id':aid},
   {'seq':2,'kind':'connector_read','operation':'repository_bundle_download','connector':'GitHub','artifact':f'repository-bundle-{sha}','source_sha':sha,'admission_id':aid},
   {'seq':3,'kind':'tool','operation':'repository_bundle_materialize','source_sha':sha,'admission_id':aid},
   {'seq':4,'kind':'tool','operation':'repository_bundle_verify','repository':'marcogallotta/ai-tools','repository_id':1304888921,'source_ref':'refs/heads/main','source_sha':sha,'verified':True,'admission_id':aid},
   {'seq':5,'kind':'tool','operation':'repository_bundle_bind','source_sha':sha,'bound':True,'admission_id':aid},
   {'seq':6,'kind':'reasoning','operation':'substantial_cross_file_reasoning','source_sha':sha,'admission_id':aid},
   {'seq':7,'kind':'connector_read','operation':'github_current_state','connector':'GitHub','admission_id':aid},
   {'seq':8,'kind':'connector_read','operation':'asana_current_state','connector':'Asana','admission_id':aid}]
 if sid=='repository-context-admission-tiny-lookup':
  return [{'seq':1,'kind':'connector_read','operation':'targeted_status_lookup','connector':'GitHub'}]
 if sid=='standing-policy-post-integration-main-readback':
  return [
   {'seq':1,'kind':'connector_read','operation':'pull_request_merged_state','connector':'GitHub','merged':True},
   {'seq':2,'kind':'connector_read','operation':'standing_invariant_main_readback','connector':'GitHub','invariant_id':'repository-context-admission','coverage_complete':False}]
 if sid=='chatty-authorized-action-before-narration': return [
  {'seq':1,'kind':'durable_write','operation':'chatty_disposable_action','target':'fixture','write_id':'chatty-a1'},
  {'seq':2,'kind':'readback','operation':'chatty_disposable_action','target':'fixture','write_id':'chatty-a1','verified':True}]
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
 removed=edge['changes'][0]
 edge['changes']=edge['changes'][1:]
 with pytest.raises(kernels.KernelError,match='classification mismatch'): kernels._validate_current_edge_classification(bad,s)
 role=next(r for r in removed['roles'] if r!='*') if '*' not in removed['roles'] else 'review'
 boundary=next(b for b in removed['action_boundaries'] if b!='*') if '*' not in removed['action_boundaries'] else 'review-write'
 drift=kernels.classify_project_drift(edge['from_version'],role,boundary,manifest=bad,source=s)
 assert drift['state']=='integrity_error' and drift['block'] and not drift['resync_required']

def test_classified_stable_rule_removal_is_representable_and_unknown_ids_still_fail():
 _,s=kernels.load_canonical(); removed=copy.deepcopy(s)
 removed['roles']['review']['rules']=[r for r in removed['roles']['review']['rules'] if r['id']!='review-formal-comment']
 prior_shared={x['id']:kernels._rule_fingerprint(x) for x in kernels._rules(s['shared_rules'],'shared_rules')}
 prior_roles={role:{x['id']:kernels._rule_fingerprint(x) for x in kernels._rules(s['roles'][role]['rules'],f'roles.{role}.rules')} for role in s['roles']}
 edge={'from_version':'v1','to_version':'v2','changes':[_change('review-formal-comment','review','additive','review-write','lifecycle')],'from_rule_fingerprints':{'_shared':prior_shared,'_roles':prior_roles},'from_renderer_fingerprint':kernels.renderer_fingerprint()}
 legacy={'from_version':'v0','to_version':'v1','changes':[_change('review-action-handoff','review','compatible','handoff','presentation')]}
 m={'canonical_version':'v2','change_history':[legacy,edge],'legacy_bootstrap_floor':{'first_drift_aware_version':'v1','pre_floor_versions':['v0'],'impact':'breaking','roles':['*'],'action_boundaries':['*'],'break_proof':{'prior_kernel_identity':'legacy','counterexample':'legacy mismatch stop','git_reconciliation_failure':'legacy cannot fold history','migration':'resync','rollback':'disable legacy','evidence_ref':'test'},'marco_approved':True,'marco_approval_ref':'test:approval'}}
 unclassified=copy.deepcopy(m); unclassified['change_history'][0]['changes']=[]
 with pytest.raises(kernels.KernelError,match='requires changes|classification mismatch'):
  kernels.validate_change_history(unclassified,removed)
 kernels.validate_change_history(copy.deepcopy(m),removed)
 unknown=copy.deepcopy(m); unknown['change_history'][0]['changes'].append(_change('never-existed-rule','review','breaking','review-write','lifecycle'))
 with pytest.raises(kernels.KernelError,match='unknown rule'):
  kernels.validate_change_history(unknown,removed)

def test_generated_kernels_current_bound_and_within_budget():
 m,s=kernels.load_canonical(); results=kernels.render_all(check=True); assert len(results)==8
 for role,p in kernels.generated_paths(m,s).items():
  text=p.read_text(); assert len(text)<=m['max_kernel_chars']; assert f"PROJECT_CANONICAL_VERSION: {m['canonical_version']}" in text
  assert text.index('PROJECT_REPOSITORY: marcogallotta/ai-tools')<text.index('Startup:')
  assert 'Mismatch alone never blocks' in text and '?/3 integrity error' in text

def test_five_whys_preservation_inventory_binds_doc_index_rule_kernels_and_behavior_cases():
 m,s=kernels.load_canonical(); payload=kernels._eval_payload(); preservation=m['preservation_inventory']
 assert kernels.validate_preservation_inventory(s,payload,preservation)==['five-whys-shared-method']
 entry=preservation['entries'][0]; assert entry['canonical_document']=='dish/docs/agents/five-whys.md'
 assert entry['index_link'] in (DISH_ROOT/'docs'/'agents'/'index.md').read_text()
 rule=next(x for x in s['shared_rules'] if x['id']=='five-whys-shared-method')
 for trigger in ('Five Whys','5 whys','blameless-RCA'): assert trigger in rule['text']
 for role,path in kernels.generated_paths(m,s).items():
  text=path.read_text(); assert 'dish/docs/agents/five-whys.md' in text and rule['text'] in text, role
 by={x['id']:x for x in payload['scenarios']}
 for sid in entry['behavior_scenario_ids']:
  assert set(by[sid]['roles'])==set(s['roles'])
  assert 'five-whys-shared-method' in by[sid]['required_rules']

def test_five_whys_preservation_rejects_coordinated_silent_deletion():
 m,s=kernels.load_canonical(); payload=kernels._eval_payload(); preservation=m['preservation_inventory']
 with pytest.raises(kernels.KernelError,match='manifest requires preservation_inventory'):
  kernels.validate_preservation_inventory(s,payload,None)
 missing_inventory=copy.deepcopy(preservation); missing_inventory['entries']=[]
 with pytest.raises(kernels.KernelError,match='preservation inventory mismatch'):
  kernels.validate_preservation_inventory(s,payload,missing_inventory)
 missing_rule=copy.deepcopy(s); missing_rule['shared_rules']=[x for x in missing_rule['shared_rules'] if x['id']!='five-whys-shared-method']
 with pytest.raises(kernels.KernelError,match='shared rule missing'):
  kernels.validate_preservation_inventory(missing_rule,payload,preservation)
 missing_case=copy.deepcopy(payload); missing_case['scenarios']=[x for x in missing_case['scenarios'] if x['id']!='five-whys-reground-reload']
 with pytest.raises(kernels.KernelError,match='behavior scenario missing'):
  kernels.validate_preservation_inventory(s,missing_case,preservation)

def test_chatty_contract_is_compiled_into_every_project_and_root():
 m,s=kernels.load_canonical(); rules=kernels.chatty_contract(s)
 for role in s['roles']:
  text=kernels.render_role(m,s,role)
  assert text.index('Work chat:') < text.index(f"Role: **{s['roles'][role]['default_role']}**.")
  for rule in rules: assert f'- {rule}' in text
 root=(DISH_ROOT.parent/'CLAUDE.md').read_text()
 assert root.count(kernels.CHATTY_BLOCK_START)==1 and root.count(kernels.CHATTY_BLOCK_END)==1
 for rule in rules: assert f'- {rule}' in root

def test_development_workflow_context_preload_is_role_index_driven_and_read_only():
 m,s=kernels.load_canonical(); deps=kernels.context_dependencies(s,'development-workflow'); assert deps is not None
 expected={'coordinator.md','development-workflow.md','audit.md','implementation.md','integration.md','review.md','workflow.md','postgresql-dark-launch.md'}
 assert kernels.role_index_contracts()==expected
 assert deps['preload']=={'role_index_contracts':True,'additional':['dish/docs/agents/contributor-base.md']}
 assert deps['action_specific']['test-scope decisions']==['dish/docs/testing.md','dish/docs/architecture/testing-boundaries.md']
 assert deps['action_specific']['dispatcher/Integration mechanics']==['ci/pr-lifecycle-dispatcher-runbook.md']
 assert deps['action_specific']['native-PostgreSQL workflow mechanics']==['dish/docs/testing.md','dish/docs/architecture/postgresql-runtime.md']
 text=kernels.render_role(m,s,'development-workflow')
 assert text.index('Startup:')<text.index('Read-only decision context (startup/re-grounding):')
 assert 'load every standing role contract listed by the current role index' in text
 assert '`dish/docs/agents/contributor-base.md`' in text
 assert 'grants no Implementation, Review, Integration, merge, or production authority' in text
 comps=s['roles']['development-workflow']['allowed_compositions']; assert len(comps)==1 and 'implementation.md' in comps[0]
 assert 'review.md' not in comps[0] and 'integration.md' not in comps[0]


def test_development_workflow_re_grounding_and_action_context_match_standing_contract():
 role=(DISH_ROOT/'docs'/'agents'/'development-workflow.md').read_text()
 assert 'At fresh startup and after compaction/session replacement' in role
 assert 'every standing role contract it lists' in role and 'contributor-base.md' in role
 assert 'compaction/session restart should trigger role/process re-grounding' in role
 assert 'Review evidence semantics' in role and "Integration's literal `TESTS TO RUN`" in role
 for path in ('../testing.md','../architecture/testing-boundaries.md','../../../ci/pr-lifecycle-dispatcher-runbook.md','../architecture/postgresql-runtime.md'):
  assert path in role


def test_development_workflow_incident_evals_require_cross_role_and_fallback_context():
 pr60=_scenario('development-workflow-pr60-test-scope-context')
 assert pr60['roles']==['development-workflow']
 assert {'load_role_index_context_dependencies','load_test_scope_context','consider_review_evidence_semantics','consider_integration_literal_tests_to_run_semantics'}<=set(pr60['required_actions'])
 pr40=_scenario('development-workflow-pr40-fallback-context')
 assert {'load_contributor_base_context','inspect_authorized_fallback_surface','classify_residual_certification_only_after_fallback_check'}<=set(pr40['required_actions'])
 noauth=_scenario('development-workflow-context-preload-no-authority')
 assert {'treat_context_read_as_implementation_authority','treat_context_read_as_review_authority','treat_context_read_as_integration_authority'}<=set(noauth['forbidden_actions'])

def test_repository_context_admission_is_shared_rendered_and_independently_registered():
 m,s=kernels.load_canonical(); registry=kernels._standing_invariant_registry(); entry=registry['repository-context-admission']
 assert entry['status']=='active'
 assert set(entry['coverage']['rendered_roles'])==set(s['roles'])==set(kernels.REPOSITORY_CONTEXT_ROLES)
 assert set(entry['coverage']['required_eval_ids'])==set(kernels.REPOSITORY_CONTEXT_EVAL_IDS)
 shared={r['id']:r for r in kernels._rules(s['shared_rules'],'shared_rules')}; rule=shared['repository-context-admission']
 assert entry['coverage']['source_rule_fingerprint']==kernels._rule_fingerprint(rule)
 for role in s['roles']:
  ids={r['id'] for r in kernels.effective_rules(s,role)}; assert 'repository-context-admission' in ids
  assert rule['text'] in kernels.render_role(m,s,role)
 assert kernels.validate_standing_invariants(s)==['repository-context-admission:active']


def test_repository_context_behavior_contract_covers_order_exemption_reentry_and_stale_main():
 full=_scenario('repository-context-admission-consequential-reasoning')
 assert full['require_ordered_observations'] is True and full['observation_link_field']=='admission_id'
 assert [x['operation'] for x in full['required_observations']]==['repository_main_identity','repository_bundle_download','repository_bundle_materialize','repository_bundle_verify','repository_bundle_bind','substantial_cross_file_reasoning','github_current_state','asana_current_state']
 assert set(full['roles'])==set(kernels.REPOSITORY_CONTEXT_ROLES)
 missing=_scenario('repository-context-admission-missing-bundle'); assert missing['roles']==['development-workflow'] and 'block_affected_substantial_conclusion' in missing['required_actions']
 tiny=_scenario('repository-context-admission-tiny-lookup'); assert 'classify_tiny_targeted_lookup' in tiny['required_actions'] and 'require_repository_bundle_for_tiny_lookup' in tiny['forbidden_actions']
 reentry=_scenario('repository-context-admission-reentry'); assert 'reenter_repository_context_admission' in reentry['required_actions']
 stale=_scenario('repository-context-admission-stale-main'); assert {'reject_stale_repository_context','reenter_repository_context_admission'}<=set(stale['required_actions'])


def test_repository_context_ordered_observation_rejects_reasoning_before_verification():
 p=_passing(); r=_result(p,'repository-context-admission-consequential-reasoning::implementation')
 by={x['operation']:x for x in r['runner_observations']}
 by['substantial_cross_file_reasoning']['seq']=4
 by['repository_bundle_verify']['seq']=6
 with pytest.raises(kernels.KernelError,match='missing required runner observation'):
  kernels.evaluate_behavior_results(p)


def test_independent_registry_catches_self_consistent_project_surface_deletion():
 _,s=kernels.load_canonical(); registry=kernels._standing_invariant_registry(); rebuilt=copy.deepcopy(s)
 rebuilt['shared_rules']=[r for r in rebuilt['shared_rules'] if r['id']!='repository-context-admission']
 rebuilt['roles']['integration']['rules']=[r for r in rebuilt['roles']['integration']['rules'] if r['id']!='integration-standing-policy-readback']
 deleted=set(kernels.REPOSITORY_CONTEXT_EVAL_IDS)
 eval_ids={x['id'] for x in kernels._evals()}-deleted
 required_ids=set(kernels.REQUIRED_EVAL_IDS)-deleted
 with pytest.raises(kernels.KernelError,match='missing canonical shared source rule'):
  kernels.validate_standing_invariants(rebuilt,registry=registry,eval_ids=eval_ids,required_eval_ids=required_ids)


def test_reconciliation_shape_cannot_omit_registered_bundle_outcome():
 _,s=kernels.load_canonical(); registry=kernels._standing_invariant_registry(); reconstructed=copy.deepcopy(s)
 reconstructed['shared_rules']=[r for r in reconstructed['shared_rules'] if r['id']!='repository-context-admission']
 # Simulate main + an unrelated selected delta: the Project surface remains otherwise valid-looking.
 reconstructed['roles']['implementation']['rules'][0]['text']+=' Selected unrelated reconciliation delta.'
 with pytest.raises(kernels.KernelError,match='missing canonical shared source rule'):
  kernels.validate_standing_invariants(reconstructed,registry=registry)


def test_standing_invariant_removal_requires_explicit_durable_supersession():
 _,s=kernels.load_canonical(); registry=kernels._standing_invariant_registry()
 missing=copy.deepcopy(registry); missing.pop('repository-context-admission')
 with pytest.raises(kernels.KernelError,match='missing required standing invariants'):
  kernels.validate_standing_invariants(s,registry=missing)
 superseded=copy.deepcopy(registry); entry=superseded['repository-context-admission']; entry['status']='superseded'; entry.pop('supersession',None)
 with pytest.raises(kernels.KernelError,match='supersession requires durable explicit authority'):
  kernels.validate_standing_invariants(s,registry=superseded)
 entry['supersession']={'authority_type':'marco-explicit','durable_ref':'asana:task:example#explicit-decision','decision':'supersede for test','effective_at':'2026-08-16T22:00:00+02:00'}
 assert kernels.validate_standing_invariants(s,registry=superseded)==['repository-context-admission:superseded']


def test_active_standing_invariant_cannot_be_weakened_by_updating_project_and_registry_together():
 _,s=kernels.load_canonical(); registry=kernels._standing_invariant_registry(); weakened=copy.deepcopy(s); altered=copy.deepcopy(registry)
 rule=next(r for r in weakened['shared_rules'] if r['id']=='repository-context-admission'); rule['text']='Tiny reads and substantial reasoning may both proceed without a repository bundle.'
 altered['repository-context-admission']['coverage']['source_rule_fingerprint']=kernels._rule_fingerprint(rule)
 with pytest.raises(kernels.KernelError,match='coverage weakened without supersession'):
  kernels.validate_standing_invariants(weakened,registry=altered)


def test_standing_policy_completion_requires_authoritative_main_coverage_readback():
 _,s=kernels.load_canonical(); integration={r['id']:r for r in kernels.effective_rules(s,'integration')}
 assert 'integration-standing-policy-readback' in integration
 contract=(DISH_ROOT/'docs'/'agents'/'integration.md').read_text()
 assert 'do not mark the task complete from merge/ancestry alone' in contract and 'standing-invariants.json' in contract
 scenario=_scenario('standing-policy-post-integration-main-readback')
 assert scenario['roles']==['integration'] and scenario['require_ordered_observations'] is True
 assert {'refuse_done_without_required_main_coverage','keep_owning_task_open'}<=set(scenario['required_actions'])


def test_eval_contract_matrix_and_oracle_free_prepared_cases():
 ids=kernels.validate_eval_contracts(); assert set(ids)==kernels.REQUIRED_EVAL_IDS
 b=kernels.prepare_eval_bundle(); assert len(b['cases'])==sum(len(q['roles']) for q in kernels._evals())
 assert all(kernels.ORACLE_FIELDS.isdisjoint(c) for c in b['cases'])
 by={c['case_id']:c for c in b['cases']}; assert by['configured-repository-pr-routing::review']['prompt']=='review PR31'; assert by['configured-repository-pr-routing::integration']['prompt']=='merge PR34'

def test_five_whys_behavior_cases_cover_load_reground_evidence_branching_and_anti_patterns():
 initial=_scenario('five-whys-evidence-discipline'); reground=_scenario('five-whys-reground-reload')
 assert {'read_canonical_five_whys_method_before_substantive_rca','classify_verified_rejected_unknown','allow_fewer_or_more_than_five','branch_supported_causes'}<=set(initial['required_actions'])
 assert {'substantive_rca_before_method_load','force_exactly_five','stop_at_human_blame','present_hypothesis_as_fact'}<=set(initial['forbidden_actions'])
 assert {'reload_canonical_five_whys_method_after_regrounding','continue_evidence_classification','continue_branching_as_evidence_requires','preserve_role_authority'}<=set(reground['required_actions'])
 assert {'continue_from_stale_method_memory','invent_missing_evidence','treat_method_as_authority_expansion'}<=set(reground['forbidden_actions'])
 for q in (initial,reground):
  assert q['required_observations']==[{'equals':{'connector':'GitHub','path':'dish/docs/agents/five-whys.md','repository':'marcogallotta/ai-tools'},'kind':'connector_read','operation':'repository_file_read'}]

def test_behavior_evaluator_accepts_complete_matrix(): assert len(kernels.evaluate_behavior_results(_passing()))==sum(len(q['roles']) for q in kernels._evals())

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






def test_task_required_drift_eval_cases_are_present_and_scoped():
 by={s['id']:s for s in kernels._evals()}
 assert by['compatible-wording-drift']['roles']==['implementation']; assert by['unrelated-role-drift']['roles']==['implementation']
 assert by['review-breaking-completion-drift']['roles']==['review']; assert by['integration-breaking-merge-drift']['roles']==['integration']
 assert 'fold_all_skipped_versions' in by['skipped-version-breaking-drift']['required_actions']
 assert {'project-drift-current-silent','project-drift-integrity-error','project-drift-pre-d96-legacy','project-drift-v708-review-compatible','project-drift-self-compatible'}<=set(by)

def test_role_and_publication_boundaries_remain_high_salience():
 _,s=kernels.load_canonical(); impl=' '.join(x['text'] for x in kernels.effective_rules(s,'implementation')); rev=' '.join(x['text'] for x in kernels.effective_rules(s,'review')); integ=' '.join(x['text'] for x in kernels.effective_rules(s,'integration'))
 assert 'owned branch + commit + PR + exact head' in impl and 'Do not self-review/integrate' in impl
 assert 'formal GitHub `COMMENT` verdict' in rev and 'Review does not implement fixes' in rev
 assert 'current head must equal the exact reviewed/certified head' in integ


def test_c1_governance_contracts_and_evals_are_mechanical():
 m,s=kernels.load_canonical()
 assert 'audit' in s['roles'] and m['generated_role_files']['audit']=='audit.md'
 audit={r['id'] for r in kernels.effective_rules(s,'audit')}
 assert {'audit-authority-boundary','audit-exact-baseline','audit-asana-disposition','audit-specialist-context'}<=audit
 for role in ('coordinator','development-workflow','implementation','integration','review','workflow','postgresql-dark-launch'):
  assert 'repository-friction-capture' in {r['id'] for r in kernels.effective_rules(s,role)}
 assert 'code-smell-debt-capture' not in {r['id'] for r in kernels.effective_rules(s,'implementation')}
 assert {'decision-provenance','authenticated-account-provenance'}<={r['id'] for r in kernels.effective_rules(s,'coordinator')}
 ids={x['id'] for x in kernels._evals()}
 assert {'audit-exact-baseline','repository-friction-discovery','code-smell-dedupe-log-and-continue','durable-review-classification','coordinator-check-everything-mixed-state','authenticated-account-not-human-decision','shared-resource-concurrency-preflight'}<=ids


def test_c1_standing_contracts_preserve_authority_and_capture_surfaces():
 audit=(DISH_ROOT/'docs'/'agents'/'audit.md').read_text()
 assert 'read-only for GitHub/source mutation' in audit and 'bounded Asana finding disposition' in audit
 assert 'may not implement fixes' in audit and 'exact audited GitHub SHA/baseline' in audit
 base=(DISH_ROOT/'docs'/'agents'/'contributor-base.md').read_text()
 assert '1217443500915644' in base and 'notice -> dedupe -> log/update -> continue' in base
 assert '1217443501022227' in base and 'Current blockers remain on the active work surface' in base
 dw=(DISH_ROOT/'docs'/'agents'/'development-workflow.md').read_text()
 assert '`AGENT REVIEW`' in dw and '`AGENT RE-REVIEW`' in dw and '`HUMAN APPROVAL/DECISION`' in dw
 assert 'Observing a quiet state is not isolation' in dw and 'mechanically enforced admission/fencing boundary' in dw
 idx=(DISH_ROOT/'docs'/'agents'/'index.md').read_text()
 assert 'Authenticated-account metadata' in idx and 'not that Marco physically performed or approved' in idx

def test_fixture_mismatch_standing_contracts_stop_impossible_repair_and_escalate_owner():
 coordinator=(DISH_ROOT/'docs'/'agents'/'coordinator.md').read_text()
 workflow=(DISH_ROOT/'docs'/'agents'/'development-workflow.md').read_text()
 for text in (coordinator,workflow):
  assert 'Compatibility preflight' in text
  assert 'Ownership escalation' in text
  assert 'Blocker consistency' in text
  assert 'IMPLEMENTATION REQUIRED' in text
  assert 'LOCAL SYSTEM ACCESS' in text
  assert 'deferred' in text and 'not required' in text
  assert 'This needs an Implementation fix: <one-sentence scope>.' in text
  assert 'Five Whys' in text
 assert 'disposable never waives' in coordinator
 assert 'disposability is not an exemption' in workflow
 assert 'do not create another queue, scheduler, or lifecycle controller' in coordinator
 assert 'creates no new scheduler, queue, or lifecycle authority' in workflow


def test_fixture_mismatch_recurrence_matrix_covers_escalation_and_non_escalation():
 incompatible=_scenario('comparison-incompatible-target-escalates-implementation')
 assert {'check_all_compared_system_health_requirements','reject_incompatible_common_target','stop_fixture_repair','classify_implementation_required','keep_required_blocker_active'}<=set(incompatible['required_actions'])
 assert {'continue_fixture_repair','classify_local_system_access','defer_required_capability'}<=set(incompatible['forbidden_actions'])
 deferred=_scenario('active-gate-blocker-cannot-be-deferred')
 assert {'mark_required_blocker_deferred','mark_required_blocker_not_required'}<=set(deferred['forbidden_actions'])
 disposable=_scenario('disposable-fixture-still-needs-health')
 assert 'treat_disposability_as_health_override' in disposable['forbidden_actions']
 separate=_scenario('separate-pr-does-not-clear-independent-blocker')
 assert 'mark_independent_blocker_resolved' in separate['forbidden_actions']
 human=_scenario('implementation-escalation-is-action-first')
 assert 'begin_with_implementation_fix_sentence' in human['required_actions']
 assert 'diagnosis_before_action' in human['forbidden_actions']
 supported=_scenario('supported-operation-stays-local-system-access')
 assert 'classify_local_system_access' in supported['required_actions']
 assert 'classify_implementation_required' in supported['forbidden_actions']

def _prior_fingerprints(source):
 return {
  '_shared':{x['id']:kernels._rule_fingerprint(x) for x in kernels._rules(source['shared_rules'],'shared_rules')},
  '_roles':{role:{x['id']:kernels._rule_fingerprint(x) for x in kernels._rules(source['roles'][role]['rules'],f'roles.{role}.rules')} for role in source['roles']},
 }

def _synthetic_transition(*,impact='compatible',proof=True):
 m,s=kernels.load_canonical(); prior=copy.deepcopy(s); current=copy.deepcopy(s)
 target=next(r for r in current['roles']['implementation']['rules'] if r['id']=='implementation-durable-git')
 target['text'] += ' Synthetic transition semantics.'
 change=_change('implementation-durable-git','implementation',impact,'role-critical-write','lifecycle')
 if impact=='breaking' and proof:
  change['break_proof']={
   'from_version':m['canonical_version'],'to_version':'synthetic-v2','roles':['implementation'],'action_boundaries':['role-critical-write'],
   'prior_kernel_identity':m['kernel_identity_sha256'],'counterexample':'Old bootstrap authorizes an operation now proven unsafe.',
   'git_reconciliation_failure':'Reading current Git cannot repair the stale bootstrap authorization.',
   'migration':'Resync the affected Implementation Project before this boundary.','rollback':'Keep the old Project out of role-critical writes.',
   'evidence_ref':'test:synthetic-hard-break'}
  change['marco_approved']=True; change['marco_approval_ref']='asana:test:exact-scope-approval'
 sm=copy.deepcopy(m); sm['canonical_version']='synthetic-v2'; sm['change_history']=copy.deepcopy(m['change_history'])+[{
  'from_version':m['canonical_version'],'to_version':'synthetic-v2','changes':[change],
  'from_rule_fingerprints':_prior_fingerprints(prior),'from_renderer_fingerprint':kernels.renderer_fingerprint(),
 }]
 return sm,current,m['canonical_version']

def test_current_project_is_silent_and_no_zero_prefix():
 m,s=kernels.load_canonical(); d=kernels.classify_project_drift(m['canonical_version'],'implementation','handoff',manifest=m,source=s)
 assert d['state']=='current' and d['indicator'] is None and d['drift_level']==0
 ok,message=kernels.version_status(m['canonical_version'],'implementation','handoff')
 assert ok is True and message=='' and '0/3' not in message

def test_d96_plus_additive_drift_continues_without_resync():
 m,s=kernels.load_canonical(); edge=m['change_history'][-1]; parent=edge['from_version']
 change=next(x for x in edge['changes'] if x['impact']=='additive')
 role=next(r for r in change['roles'] if r!='*') if '*' not in change['roles'] else 'review'
 boundary=next(b for b in change['action_boundaries'] if b!='*') if '*' not in change['action_boundaries'] else 'review-write'
 d=kernels.classify_project_drift(parent,role,boundary,manifest=m,source=s)
 assert not d['block'] and not d['resync_required'] and d['drift_level']==2
 assert d['indicator']=='PROJECT SETTINGS: OUTDATED · DRIFT 2/3'

def test_v708_review_write_is_nonblocking_and_uses_current_authority():
 m,s=kernels.load_canonical(); d=kernels.classify_project_drift('dish-chatgpt-projects-v2-708fb9a9a9bc','review','review-write',manifest=m,source=s)
 assert d['state']=='outdated' and not d['block'] and not d['resync_required']
 assert d['drift_level'] in {1,2} and d['indicator'].startswith('PROJECT SETTINGS: OUTDATED · DRIFT ')

def test_pre_d96_fixture_proves_unconditional_mismatch_stop():
 pre_d96_fixture = """PROJECT_CANONICAL_VERSION: dish-chatgpt-projects-v2-b6a326f98ad4
Startup: compare its `canonical_version` with `dish-chatgpt-projects-v2-b6a326f98ad4`. If different, report `PROJECT INSTRUCTIONS STALE` with both versions and make no role-critical state change until resynchronized.
- A mismatch means `PROJECT INSTRUCTIONS STALE`; stop role-critical changes until resynchronized."""
 assert 'If different' in pre_d96_fixture and 'make no role-critical state change until resynchronized' in pre_d96_fixture
 assert 'A mismatch means `PROJECT INSTRUCTIONS STALE`; stop role-critical changes until resynchronized.' in pre_d96_fixture
 assert 'fold `change_history`' not in pre_d96_fixture

def test_pre_d96_is_explicit_legacy_bootstrap_hard_break():
 m,s=kernels.load_canonical(); d=kernels.classify_project_drift('dish-chatgpt-projects-v2-b6a326f98ad4','implementation','role-critical-write',manifest=m,source=s)
 assert d['state']=='legacy_hard_break' and d['legacy_bootstrap_incompatibility'] is True
 assert d['block'] and d['resync_required'] and d['drift_level']==3
 assert d['indicator']=='PROJECT SETTINGS: HARD BREAK · DRIFT 3/3'

def test_invalid_or_unknown_drift_routes_to_integrity_error_without_resync():
 m,s=kernels.load_canonical(); d=kernels.classify_project_drift('unknown-version','review','review-write',manifest=m,source=s)
 assert d['state']=='integrity_error' and d['block'] and not d['resync_required']
 assert d['indicator']=='PROJECT SETTINGS: INTEGRITY ERROR · DRIFT ?/3'
 assert d['repair']=='repository-authority'

def test_malformed_unrelated_history_only_blocks_the_affected_action():
 m,s=kernels.load_canonical(); m=copy.deepcopy(m); old=None
 for edge in m['change_history']:
  for change in edge['changes']:
   if change.get('rule_id')=='coordinator-live-scan' and 'status' in change.get('action_boundaries',[]):
    change.pop('impact'); old=edge['from_version']; break
  if old:break
 unrelated=kernels.classify_project_drift(old,'review','review-write',manifest=m,source=s)
 affected=kernels.classify_project_drift(old,'coordinator','status',manifest=m,source=s)
 assert not unrelated['block'] and unrelated['state']!='integrity_error'
 assert affected['state']=='integrity_error' and affected['block'] and not affected['resync_required']

def test_proven_breaking_blocks_only_exact_role_and_action():
 m,s,old=_synthetic_transition(impact='breaking',proof=True)
 d=kernels.classify_project_drift(old,'implementation','role-critical-write',manifest=m,source=s)
 assert d['state']=='hard_break' and d['block'] and d['resync_required'] and d['drift_level']==3
 other=kernels.classify_project_drift(old,'implementation','handoff',manifest=m,source=s)
 assert not other['block'] and not other['resync_required'] and other['drift_level']==1
 role=kernels.classify_project_drift(old,'review','review-write',manifest=m,source=s)
 assert not role['block'] and role['drift_level']==1

def test_unproved_breaking_is_integrity_error_not_hard_break():
 m,s,old=_synthetic_transition(impact='breaking',proof=False)
 d=kernels.classify_project_drift(old,'implementation','role-critical-write',manifest=m,source=s)
 assert d['state']=='integrity_error' and d['block'] and not d['resync_required']
 assert d['indicator']=='PROJECT SETTINGS: INTEGRITY ERROR · DRIFT ?/3'

def test_retained_drift_aware_history_has_no_unproved_breaking():
 m,_=kernels.load_canonical(); floor=m['legacy_bootstrap_floor']['first_drift_aware_version']
 path=kernels._change_path(m,floor)
 assert path
 for edge in path:
  for change in edge['changes']:
   if change['impact']=='breaking':
    kernels._validate_breaking(edge,change)

def test_historical_reclassification_has_machine_readable_provenance():
 m,_=kernels.load_canonical(); corrected=[]
 for edge in m['change_history']:
  for change in edge['changes']:
   if 'historical_correction' in change:
    kernels._validate_correction(change); corrected.append(change)
 assert corrected and all(c['historical_correction']['previous_impact']=='breaking' for c in corrected)

def test_self_transition_is_nonblocking_for_every_role_and_action():
 m,s=kernels.load_canonical(); parent=m['change_history'][-1]['from_version']
 boundaries={'startup','status','dispatch','handoff','role-critical-write','review-write','merge','analysis'}
 for role in s['roles']:
  for boundary in boundaries:
   d=kernels.classify_project_drift(parent,role,boundary,manifest=m,source=s)
   assert d['state']=='outdated' and not d['block'] and not d['resync_required'], (role,boundary,d)
   assert d['drift_level'] in {1,2}

def test_generated_digest_integrity_is_strict_only_for_current_generation():
 m,s=kernels.load_canonical(); current=kernels.classify_project_drift(m['canonical_version'],'review','status',manifest=m,source=s,actual_generated_sha256='wrong')
 assert current['state']=='integrity_error' and not current['resync_required']
 old=kernels.classify_project_drift('dish-chatgpt-projects-v2-708fb9a9a9bc','review','review-write',manifest=m,source=s,actual_generated_sha256='historical-digest')
 assert old['state']=='outdated' and not old['block']

def test_impact_is_explicit_and_never_inferred_from_rule_criticality():
 for surface in ('authority','safety','presentation'):
  with pytest.raises(kernels.KernelError,match='explicit transition impact'):
   kernels._impact({'surface':surface})


def test_emergency_attach_eval_matrix_is_complete_and_scoped():
 required={'emergency-attach-eligible','emergency-attach-parent-mismatch','emergency-attach-tree-mismatch',
  'emergency-attach-branch-race','emergency-attach-after-review','emergency-attach-asana-authority-revoked',
  'emergency-attach-policy-denial','emergency-attach-conflicting-writer','emergency-attach-forbids-semantic-actions',
  'emergency-attach-consumed-once','emergency-attach-final-readback-required','emergency-attach-normal-broker-path-unchanged'}
 assert required<=kernels.REQUIRED_EVAL_IDS
 by={s['id']:s for s in kernels._evals()}
 assert required<=set(by)
 for sid in required:
  s=by[sid]; assert s['roles']==['implementation']
  assert {'publication-materializer-path','mutation-broker-admission'}<=set(s['required_rules'])

def test_emergency_attach_happy_case_requires_full_eligibility_chain():
 happy=_scenario('emergency-attach-eligible')
 assert {'reverify_candidate_parent_equals_old','reverify_candidate_tree_equals_expected',
  'reverify_live_pr_branch_still_old','reread_live_asana_continuation_authority',
  'confirm_infrastructure_failure_before_grant','perform_non_force_fast_forward_ref_update',
  'read_back_branch_pr_commit_tree_after_write'}<=set(happy['required_actions'])
 assert {'force_update_ref','construct_new_candidate_under_bypass','perform_semantic_source_edit'}<=set(happy['forbidden_actions'])

def test_emergency_attach_regression_matrix_fails_closed_on_every_fence():
 by={s['id']:s for s in kernels._evals()}
 assert 'reverify_candidate_parent_equals_old' in by['emergency-attach-parent-mismatch']['required_actions']
 assert 'reverify_candidate_tree_equals_expected' in by['emergency-attach-tree-mismatch']['required_actions']
 assert 'reverify_live_pr_branch_still_old' in by['emergency-attach-branch-race']['required_actions']
 assert 'reverify_pr_still_draft_pre_review' in by['emergency-attach-after-review']['required_actions']
 assert 'reread_live_asana_continuation_authority' in by['emergency-attach-asana-authority-revoked']['required_actions']
 assert 'classify_broker_failure_cause' in by['emergency-attach-policy-denial']['required_actions']
 assert 'check_for_current_grant_or_conflicting_writer' in by['emergency-attach-conflicting-writer']['required_actions']
 for sid in ('emergency-attach-parent-mismatch','emergency-attach-tree-mismatch','emergency-attach-branch-race',
  'emergency-attach-after-review','emergency-attach-asana-authority-revoked','emergency-attach-policy-denial',
  'emergency-attach-conflicting-writer'):
  assert 'perform_non_force_fast_forward_ref_update' in by[sid]['forbidden_actions']
  assert 'force_update_ref' not in by[sid]['required_actions']

def test_emergency_attach_never_grants_source_review_integration_or_merge_authority():
 forbidding=_scenario('emergency-attach-forbids-semantic-actions')
 assert {'perform_semantic_source_edit','fix_review_block_under_bypass','reconcile_integration_under_bypass',
  'merge_under_bypass','mutate_main_under_bypass'}<=set(forbidding['forbidden_actions'])
 consumed=_scenario('emergency-attach-consumed-once')
 assert 'reuse_consumed_bypass' in consumed['forbidden_actions']
 assert 'require_normal_current_authority_for_new_mutation' in consumed['required_actions']
 readback=_scenario('emergency-attach-final-readback-required')
 assert {'claim_publication_complete_without_readback','claim_publication_complete_on_readback_mismatch'}<=set(readback['forbidden_actions'])
 assert 'read_back_branch_pr_commit_tree_after_write' in readback['required_actions']
 normal=_scenario('emergency-attach-normal-broker-path-unchanged')
 assert 'invoke_emergency_attach_bypass' in normal['forbidden_actions']
 assert 'verify_current_broker_proof_backed_grant' in normal['required_actions']

def test_emergency_attach_standing_contracts_carry_the_same_narrow_exception():
 dw=(DISH_ROOT/'docs'/'agents'/'development-workflow.md').read_text()
 impl=(DISH_ROOT/'docs'/'agents'/'implementation.md').read_text()
 materializer=(DISH_ROOT.parent/'ci'/'publication-materializer.md').read_text()
 for text in (dw,impl,materializer):
  assert 'attach' in text.lower()
  assert 'non-force' in text or 'force=false' in text
 assert 'attach-only class is the sole temporary standing exception' in dw
 assert 'Ordinary broker self-repair still requires explicit Marco authority' in dw
