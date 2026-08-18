#!/usr/bin/env bash
set -euo pipefail
: "${BASE_SHA:?BASE_SHA required}"
cp dish/docs/chatgpt-projects/source.json /tmp/task1-base-source.json
cp dish/docs/chatgpt-projects/manifest.json /tmp/task1-base-manifest.json
python3 <<'PY'
from pathlib import Path
import hashlib, json

source_path=Path('dish/docs/chatgpt-projects/source.json')
source=json.loads(source_path.read_text())
hits=[]
def walk(v):
    if isinstance(v,dict):
        if v.get('id')=='repository-context-admission': hits.append(v)
        for x in v.values(): walk(x)
    elif isinstance(v,list):
        for x in v: walk(x)
walk(source)
if len(hits)!=1: raise SystemExit(f'expected one repository-context-admission rule, got {len(hits)}')
rule=hits[0]
old=rule['text']
if 'Missing/unverifiable/stale context blocks only the affected substantial conclusion.' not in old:
    raise SystemExit('repository-context-admission baseline text changed')
rule['text']=(
    'Before substantial consequential repository/system reasoning outside ordinary ChatGPT PR Review, establish a current repository-context witness: resolve live `refs/heads/main` plus repository name/ID from GitHub; retrieve the exact `repository-bundle-<SHA>` through the GitHub connector; materialize it; verify with `scripts/repository_bundle.py` against name/ID/ref/SHA; bind the verified clone; only then reason across files. Ordinary ChatGPT PR Review instead applies `review-bundle-fallback` when the exact bundle cannot be discovered or retrieved but connector-native exact evidence is sufficient. Any bundle actually used by Review remains subject to exact name/ID/ref/SHA verification and stale/mismatched/corrupt/wrong-SHA rejection. Tiny targeted reads are exempt. Re-enter after fresh/replacement session, post-compaction re-ground, affected-role switch, or main movement whenever the witness is absent/stale. Missing/unverifiable/stale context blocks only the affected substantial non-Review conclusion; ordinary Review fails closed only on a named unresolved semantic/tool/environment evidence boundary that connector-native evidence cannot satisfy. Bundle is read-only context; live GitHub/Asana remain current-state authorities.'
)
source_path.write_text(json.dumps(source,indent=2,sort_keys=True)+'\n')

payload={k:rule.get(k) for k in ('id','text','impact','surface','action_boundaries')}
fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()

standing_path=Path('dish/docs/agents/standing-invariants.json')
standing=json.loads(standing_path.read_text())
entries=[x for x in standing['invariants'] if x.get('id')=='repository-context-admission']
if len(entries)!=1: raise SystemExit('standing repository-context-admission entry missing/ambiguous')
entry=entries[0]
entry['ratification']['decision']='Consequential ChatGPT repository/system reasoning outside ordinary ChatGPT PR Review uses the verified exact-current-main repository bundle before substantial cross-file reasoning; ordinary Review may use sufficient connector-native exact evidence when bundle transport is unavailable, while any bundle actually used remains exact-current verified.'
refs=entry['ratification']['durable_authority_refs']
if 'asana:task:1217594495187308' not in refs: refs.append('asana:task:1217594495187308')
entry['semantic_contract']['ordinary_chatgpt_pr_review']={
    'bundle_unavailable':'connector-native-exact-evidence-fallback',
    'bundle_used':'exact-current-validation-required'
}
entry['coverage']['source_rule_fingerprint']=fingerprint
standing_path.write_text(json.dumps(standing,indent=2,sort_keys=True)+'\n')

script_path=Path('dish/scripts/chatgpt_project_kernels.py')
script=script_path.read_text()
old_refs="REPOSITORY_CONTEXT_RATIFICATION_REFS=('asana:task:1217508843698365','asana:task:1217508843698365#story:1217509740007539')"
new_refs="REPOSITORY_CONTEXT_RATIFICATION_REFS=('asana:task:1217508843698365','asana:task:1217508843698365#story:1217509740007539','asana:task:1217594495187308')"
if script.count(old_refs)!=1: raise SystemExit('ratification refs baseline changed')
script=script.replace(old_refs,new_refs,1)
old_fp="REPOSITORY_CONTEXT_SOURCE_RULE_FINGERPRINT='45190e0b9e9ffe3f4f8f33141f7cdb4e16572c90eb5453f2d2a9e11768734e3d'"
new_fp=f"REPOSITORY_CONTEXT_SOURCE_RULE_FINGERPRINT='{fingerprint}'"
if script.count(old_fp)!=1: raise SystemExit('repository-context fingerprint baseline changed')
script=script.replace(old_fp,new_fp,1)
old_semantic="expected_semantic={'admission_order':list(REPOSITORY_CONTEXT_ADMISSION_ORDER),'tiny_targeted_reads_exempt':True,'reentry_events':['fresh-or-replacement-session','post-compaction-reground','affected-role-switch','main-movement-with-absent-or-stale-witness'],'failure_scope':'affected-substantial-conclusion-only','bundle_authority':'read-only-context','current_state_authorities':['GitHub','Asana']}"
new_semantic="expected_semantic={'admission_order':list(REPOSITORY_CONTEXT_ADMISSION_ORDER),'tiny_targeted_reads_exempt':True,'reentry_events':['fresh-or-replacement-session','post-compaction-reground','affected-role-switch','main-movement-with-absent-or-stale-witness'],'failure_scope':'affected-substantial-conclusion-only','bundle_authority':'read-only-context','current_state_authorities':['GitHub','Asana'],'ordinary_chatgpt_pr_review':{'bundle_unavailable':'connector-native-exact-evidence-fallback','bundle_used':'exact-current-validation-required'}}"
if script.count(old_semantic)!=1: raise SystemExit('standing semantic baseline changed')
script_path.write_text(script.replace(old_semantic,new_semantic,1))

root_path=Path('CLAUDE.md')
root=root_path.read_text()
old_intro='For substantial repository/system reasoning that can affect a consequential Dish decision, use the repository bundle first. Tiny targeted lookups that do not support such reasoning are exempt:'
new_intro='For substantial repository/system reasoning that can affect a consequential Dish decision **outside ordinary ChatGPT PR Review**, use the repository bundle first. Tiny targeted lookups that do not support such reasoning are exempt. Ordinary ChatGPT PR Review follows the Review-specific connector-native fallback when the exact bundle cannot be discovered or retrieved; any bundle actually used still must pass exact-current identity/integrity validation:'
if root.count(old_intro)!=1: raise SystemExit('root bundle intro baseline changed')
root=root.replace(old_intro,new_intro,1)
old_fail='- fail closed if connector retrieval, materialization, checksum/manifest verification, `git bundle verify`, advertised-main validation, or cloned-HEAD validation fails.'
new_fail='- outside ordinary ChatGPT PR Review, fail closed if connector retrieval, materialization, checksum/manifest verification, `git bundle verify`, advertised-main validation, or cloned-HEAD validation fails; ordinary Review treats bundle unavailability alone as non-blocking when connector-native exact evidence is sufficient.'
if root.count(old_fail)!=1: raise SystemExit('root bundle fail-closed baseline changed')
root=root.replace(old_fail,new_fail,1)
anchor='The publication and verification contract, cadence, retention, and v1 main-only scope are in [`ci/repository-bundle.md`](ci/repository-bundle.md).'
carve=('Ordinary ChatGPT PR Review therefore never blocks, routes local, or asks Marco solely for bundle transport when live connector-native exact evidence is sufficient for the merge question. If a bundle is actually used, stale, mismatched, corrupt, or wrong-SHA material remains fail-closed and is never substituted. Review fails closed only on a named unresolved semantic, tool, or environment evidence boundary that connector-native evidence cannot satisfy. This exception does not relax bundle-first admission for broad architecture, Implementation, or other substantial non-Review reasoning.\n\n')
if root.count(anchor)!=1: raise SystemExit('root bundle publication anchor changed')
root_path.write_text(root.replace(anchor,carve+anchor,1))

test_path=Path('dish/tests/test_review_bundle_consistency.py')
test_path.write_text('''from __future__ import annotations
import importlib.util
from pathlib import Path

DISH_ROOT=Path(__file__).resolve().parents[1]
REPO_ROOT=DISH_ROOT.parent
SCRIPT=DISH_ROOT/'scripts'/'chatgpt_project_kernels.py'
SPEC=importlib.util.spec_from_file_location('chatgpt_project_kernels_review_bundle',SCRIPT); assert SPEC and SPEC.loader
kernels=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(kernels)

def _rule(source,rid):
    matches=[x for x in kernels.effective_rules(source,'review') if x['id']==rid]
    assert len(matches)==1
    return matches[0]

def test_rendered_review_bundle_instructions_are_noncontradictory():
    manifest,source=kernels.load_canonical()
    admission=_rule(source,'repository-context-admission')
    fallback=_rule(source,'review-bundle-fallback')
    rendered=kernels.render_role(manifest,source,'review')
    assert 'outside ordinary ChatGPT PR Review' in admission['text']
    assert 'ordinary Review fails closed only on a named unresolved semantic/tool/environment evidence boundary' in admission['text']
    assert 'Missing/unverifiable/stale context blocks only the affected substantial conclusion.' not in admission['text']
    assert admission['text'] in rendered
    assert fallback['text'] in rendered
    assert 'Never block, route local, or ask Marco solely for bundle transport.' in fallback['text']

def test_root_bootstrap_has_the_same_review_carveout_and_invalid_bundle_fence():
    root=(REPO_ROOT/'CLAUDE.md').read_text()
    assert '**outside ordinary ChatGPT PR Review**' in root
    assert 'ordinary Review treats bundle unavailability alone as non-blocking when connector-native exact evidence is sufficient' in root
    assert 'stale, mismatched, corrupt, or wrong-SHA material remains fail-closed' in root
    assert 'does not relax bundle-first admission for broad architecture, Implementation, or other substantial non-Review reasoning' in root

def test_nonreview_roles_still_receive_bundle_first_admission():
    manifest,source=kernels.load_canonical()
    admission=_rule(source,'repository-context-admission')['text']
    for role in source['roles']:
        if role=='review':
            continue
        rendered=kernels.render_role(manifest,source,role)
        assert admission in rendered
        assert 'Before substantial consequential repository/system reasoning outside ordinary ChatGPT PR Review' in rendered

def test_bundle_unavailable_behavior_case_requires_progress_without_human_transport_action():
    scenario=next(x for x in kernels._evals() if x['id']=='review-bundle-unavailable-proceeds')
    assert scenario['roles']==['review']
    assert {'resolve-exact-pr-and-live-authority','use-connector-native-review-evidence','proceed-substantive-review'} <= set(scenario['required_actions'])
    assert {'block-for-bundle-transport-only','route-local-for-bundle-transport-only','ask-marco-for-bundle-waiver-or-relay'} <= set(scenario['forbidden_actions'])
''')
PY

python3 dish/scripts/chatgpt_project_kernels.py reconcile \
  --base-manifest /tmp/task1-base-manifest.json \
  --base-source /tmp/task1-base-source.json \
  --source dish/docs/chatgpt-projects/source.json \
  --output dish/docs/chatgpt-projects/manifest.json
python3 dish/scripts/chatgpt_project_kernels.py render
python3 dish/scripts/chatgpt_project_kernels.py check
(cd dish && .venv/bin/python -m pytest -q tests/test_chatgpt_project_kernels.py tests/test_review_bundle_consistency.py)
git diff --check

git config user.name 'Dish Agent'
git config user.email 'dish-agent@users.noreply.github.com'
git add CLAUDE.md dish/docs/agents/standing-invariants.json dish/docs/chatgpt-projects dish/scripts/chatgpt_project_kernels.py dish/tests/test_review_bundle_consistency.py
git commit -m $'Resolve Review bundle admission contradiction\n\nAsana task: 1217594495187308'
echo "head=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"
python3 - <<'PY' >> "$GITHUB_OUTPUT"
import json
print('version='+json.load(open('dish/docs/chatgpt-projects/manifest.json'))['canonical_version'])
PY
