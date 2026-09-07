from __future__ import annotations
import importlib.util
from pathlib import Path

DISH_ROOT=Path(__file__).resolve().parents[1]
SCRIPT=DISH_ROOT/'scripts'/'chatgpt_project_kernels.py'
SPEC=importlib.util.spec_from_file_location('chatgpt_project_kernels_fast_track',SCRIPT); assert SPEC and SPEC.loader
kernels=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(kernels)

def test_retired_project_overlay_has_no_registry_or_renderer_api():
    assert not (DISH_ROOT/'docs'/'chatgpt-projects'/'fast-track-gates.json').exists()
    project_readme=(DISH_ROOT/'docs'/'chatgpt-projects'/'README.md').read_text()
    assert 'fast-track overlay' not in project_readme
    assert 'overlay reason text' not in project_readme
    for name in (
        'FAST_TRACK_GATE_REGISTRY_PATH',
        'FAST_TRACK_OVERLAY_HEADER',
        'canonical_fast_track_overlay',
        'fast_track_gate_registry',
        'fast_track_use',
        'parse_fast_track_overlay_block',
        'project_settings_compatibility_overlay',
        'render_fast_track_overlay_block',
    ):
        assert not hasattr(kernels,name)


def test_fast_track_policy_routes_to_the_current_trivial_procedure():
    moved=(DISH_ROOT/'docs'/'agents'/'fast-track-process.md').read_text()
    procedure=(DISH_ROOT/'docs'/'agents'/'trivial-fast-track.md').read_text()
    assert 'never-used ChatGPT Project gate-overlay mechanism' in moved
    assert 'TRIVIAL' in procedure and 'FAST-TRACK' in procedure

    manifest,source=kernels.load_canonical()
    for role in source['roles']:
        rendered=kernels.render_role(manifest,source,role)
        assert 'Fast-track: read triggered Procedure.' in rendered


def _worker_profile():
    manifest,source=kernels.load_canonical()
    return kernels.generated_profile_paths(manifest,source)['worker'].read_text()


def test_manual_worker_profile_size_and_exact_modes():
    profile=_worker_profile()
    assert len(profile) <= 8000
    assert 'Exactly one semantic mode is active at a time: **Implementation**, **Code Review**, **Design Review**, or **Audit**.' in profile
    assert 'Integration/merge/deploy/cutover are outside Worker.' in profile
    assert 'not a ninth semantic role' in profile


def test_manual_worker_profile_requires_same_worker_block_fix_without_automated_provenance():
    profile=_worker_profile()
    assert 'same Worker MUST explicitly switch to Implementation' in profile
    assert 'Without another Marco prompt' in profile
    assert 'does **not** require Workspace-Agent launch' in profile
    assert 'their absence never gates the ordinary manual Project-chat path' in profile
    assert 'fresh Worker performs the next Review' in profile


def test_manual_worker_profile_preserves_memory_based_no_self_review():
    profile=_worker_profile()
    assert 'may not independently Review that head while it remembers or can recover that authorship' in profile
    assert 'Genuine later compaction/forgetting follows Marco' in profile
    assert 'no durable chat-taint/provenance machinery' in profile


def test_manual_worker_design_review_stays_exact_snapshot_bound():
    profile=_worker_profile()
    for phrase in (
        'SHA-256 of exact canonical task notes/design snapshot',
        'immediately before publishing `VERDICT: PASS` or `VERDICT: BLOCK`, reread the canonical task',
        'publish no verdict for the new candidate',
        'Chat-only verdict does not count.',
    ):
        assert phrase in profile
