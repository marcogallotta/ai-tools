from __future__ import annotations
import importlib.util
from pathlib import Path
DISH_ROOT=Path(__file__).resolve().parents[1]
REPO_ROOT=DISH_ROOT.parent
SCRIPT=DISH_ROOT/'scripts'/'chatgpt_project_kernels.py'
SPEC=importlib.util.spec_from_file_location('chatgpt_project_kernels_review_bundle',SCRIPT); assert SPEC and SPEC.loader
kernels=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(kernels)
def _rule(source,rid):
    matches=[x for x in kernels.effective_rules(source,'review') if x['id']==rid]; assert len(matches)==1; return matches[0]
def test_rendered_review_bundle_instructions_are_noncontradictory():
    manifest,source=kernels.load_canonical(); admission=_rule(source,'repository-context-admission'); fallback=_rule(source,'review-bundle-fallback'); rendered=kernels.render_role(manifest,source,'review')
    assert admission['text'].startswith('Outside ordinary ChatGPT PR Review')
    assert 'Review blocks only on a named semantic/tool/environment evidence gap' in admission['text']
    assert 'Missing/unverifiable/stale context blocks only the affected substantial conclusion.' not in admission['text']
    assert admission['text'] in rendered and fallback['text'] in rendered
    assert 'Never block, route local, or ask Marco solely for bundle transport.' in fallback['text']
def test_root_bootstrap_has_the_same_review_carveout_and_invalid_bundle_fence():
    root=(REPO_ROOT/'CLAUDE.md').read_text()
    assert '**outside ordinary ChatGPT PR Review**' in root
    assert 'ordinary Review treats bundle unavailability alone as non-blocking when connector-native exact evidence is sufficient' in root
    assert 'stale, mismatched, corrupt, or wrong-SHA material remains fail-closed' in root
    assert 'does not relax bundle-first admission for broad architecture, Implementation, or other substantial non-Review reasoning' in root
def test_nonreview_roles_still_receive_bundle_first_admission():
    manifest,source=kernels.load_canonical(); admission=_rule(source,'repository-context-admission')['text']
    for role in source['roles']:
        if role=='review': continue
        rendered=kernels.render_role(manifest,source,role); assert admission in rendered
        assert 'substantial cross-file repository/system reasoning requires a verified exact-current-main bundle' in rendered
def test_bundle_unavailable_behavior_case_requires_progress_without_human_transport_action():
    scenario=next(x for x in kernels._evals() if x['id']=='review-bundle-unavailable-proceeds')
    assert scenario['roles']==['review']
    assert {'resolve-exact-pr-and-live-authority','use-connector-native-review-evidence','proceed-substantive-review'} <= set(scenario['required_actions'])
    assert {'block-for-bundle-transport-only','route-local-for-bundle-transport-only','ask-marco-for-bundle-waiver-or-relay'} <= set(scenario['forbidden_actions'])
