from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'repository_bundle.py'
WORKFLOW = Path(__file__).resolve().parents[2] / '.github' / 'workflows' / 'repository-bundle.yml'
REFRESH_WORKFLOW = Path(__file__).resolve().parents[2] / '.github' / 'workflows' / 'repository-bundle-mirror-refresh.yml'
SCHEDULE_WORKFLOW = Path(__file__).resolve().parents[2] / '.github' / 'workflows' / 'repository-bundle-mirror-schedule.yml'
RECOVERY_WORKFLOW = Path(__file__).resolve().parents[2] / '.github' / 'workflows' / 'repository-bundle-mirror-recovery.yml'
REFRESH_REQUEST_SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'repository_bundle_refresh_request.py'
REPOSITORY = 'marcogallotta/ai-tools'
REPOSITORY_ID = '1304888921'
MAIN_REF = 'refs/heads/main'


def run(*args: str, cwd: Path | None = None, check: bool = True):
    completed = subprocess.run(
        [*args], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    if check and completed.returncode != 0:
        raise AssertionError(f"command failed: {args}\nstdout={completed.stdout}\nstderr={completed.stderr}")
    return completed


def git(cwd: Path, *args: str) -> str:
    return run('git', *args, cwd=cwd).stdout.strip()


def make_repo(tmp_path: Path):
    remote = tmp_path / 'remote.git'
    source = tmp_path / 'source'
    run('git', 'init', '--bare', str(remote))
    run('git', 'init', str(source))
    git(source, 'config', 'user.name', 'Test')
    git(source, 'config', 'user.email', 'test@example.com')
    (source / 'hello.txt').write_text('hello\n', encoding='utf-8')
    git(source, 'add', 'hello.txt')
    git(source, 'commit', '-m', 'initial')
    git(source, 'branch', '-M', 'main')
    git(source, 'remote', 'add', 'origin', str(remote))
    git(source, 'push', '-u', 'origin', 'main')
    return source, remote, git(source, 'rev-parse', 'HEAD')


def build(tmp_path: Path, source: Path, sha: str):
    output = tmp_path / 'bundle'
    completed = run(
        sys.executable, str(SCRIPT), 'build',
        '--repo-root', str(source), '--output-dir', str(output),
        '--repository', REPOSITORY, '--repository-id', REPOSITORY_ID,
        '--source-sha', sha, '--source-ref', MAIN_REF,
        '--event-name', 'push', '--event-ref', MAIN_REF, '--event-sha', sha,
        '--workflow', 'Repository bundle publication', '--workflow-ref', 'workflow@main',
        '--workflow-sha', sha, '--run-id', '123', '--run-attempt', '1',
    )
    payload = json.loads(completed.stdout)
    return output, payload


def verify(tmp_path: Path, output: Path, sha: str, **overrides):
    prefix = f'repository-bundle-{sha}'
    params = {
        'bundle': output / f'{prefix}.bundle',
        'manifest': output / f'{prefix}.manifest.json',
        'checksum': output / f'{prefix}.bundle.sha256',
        'repository': REPOSITORY,
        'repository_id': REPOSITORY_ID,
        'sha': sha,
        'ref': MAIN_REF,
        'clone': tmp_path / 'clone',
    }
    params.update(overrides)
    return run(
        sys.executable, str(SCRIPT), 'verify',
        '--bundle', str(params['bundle']), '--manifest', str(params['manifest']),
        '--checksum', str(params['checksum']), '--expected-repository', str(params['repository']),
        '--expected-repository-id', str(params['repository_id']), '--expected-sha', str(params['sha']),
        '--expected-ref', str(params['ref']), '--clone-dir', str(params['clone']), check=False,
    )


def test_publication_permissions_stay_narrow():
    workflow = WORKFLOW.read_text(encoding='utf-8')
    assert 'permissions:\n  contents: read\n' in workflow
    assert workflow.count('contents: write') == 2

    build_job = workflow.split('  build-verify-artifact:\n', 1)[1].split(
        '  publish-release:\n', 1
    )[0]
    assert 'permissions:\n      contents: read\n      statuses: write\n' in build_job
    assert 'contents: write' not in build_job

    publish_job = workflow.split('  publish-release:\n', 1)[1].split(
        '  prune-old-releases:\n', 1
    )[0]
    assert "if: github.ref == 'refs/heads/main'" in publish_job
    assert 'permissions:\n      actions: read\n      contents: write\n' in publish_job

    final_job = workflow.split('  publish-final-status:\n', 1)[1]
    assert 'permissions:\n      statuses: write\n' in final_job
    assert 'contents: write' not in final_job


def test_source_sha_selection_is_bound_to_exact_main_event():
    workflow = WORKFLOW.read_text(encoding='utf-8')
    build_job = workflow.split('  build-verify-artifact:\n', 1)[1].split(
        '  publish-release:\n', 1
    )[0]
    assert 'pull_request:' not in workflow.split('permissions:', 1)[0]
    assert 'source_sha="$GITHUB_SHA"' in build_job
    assert 'if [[ "$GITHUB_REF" != "$SOURCE_REF" ]]' in build_job
    assert '--event-name "$GITHUB_EVENT_NAME"' in build_job
    assert '--event-ref "$GITHUB_REF"' in build_job
    assert '--event-sha "$GITHUB_SHA"' in build_job
    assert 'source_sha="$(git rev-parse FETCH_HEAD)"' not in build_job
    assert 'git update-ref "$SOURCE_REF" "$source_sha"' in build_job


def test_publication_concurrency_is_scoped_to_exact_sha():
    workflow = WORKFLOW.read_text(encoding='utf-8')
    assert 'group: repository-bundle-publication-${{ github.sha }}' in workflow
    publish_job = workflow.split('  publish-release:\n', 1)[1].split(
        '  prune-old-releases:\n', 1
    )[0]
    assert 'group: repository-bundle-release-publication' in publish_job
    assert 'cancel-in-progress: false' in publish_job


def test_commit_status_binds_exact_commit_to_actions_run():
    workflow = WORKFLOW.read_text(encoding='utf-8')
    assert workflow.count('"/repos/$GITHUB_REPOSITORY/statuses/$GITHUB_SHA"') == 2
    assert workflow.count("-f context='Dish / repository bundle'") == 2
    target = '-f target_url="$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"'
    assert workflow.count(target) == 2
    assert '-f state=pending' in workflow

    final_job = workflow.split('  publish-final-status:\n', 1)[1]
    assert 'if: always()' in final_job
    assert 'BUILD_RESULT: ${{ needs.build-verify-artifact.result }}' in final_job
    assert 'RELEASE_RESULT: ${{ needs.publish-release.result }}' in final_job
    assert 'if [[ "$BUILD_RESULT" == success && "$RELEASE_RESULT" == success ]]' in final_job
    assert "description='Exact repository bundle published'" in final_job
    assert "description='Repository bundle publication failed'" in final_job



def test_publication_status_is_independent_from_prune_maintenance():
    workflow = WORKFLOW.read_text(encoding='utf-8')
    final_job = workflow.split('  publish-final-status:\n', 1)[1]
    assert 'needs:\n      - build-verify-artifact\n      - publish-release\n' in final_job
    assert 'prune-old-releases' not in final_job
    prune_job = workflow.split('  prune-old-releases:\n', 1)[1].split('  publish-final-status:\n', 1)[0]
    assert 'protected_tag="repository-bundle-$current_main_sha"' in prune_job
    assert 'if [[ "$tag" == "$protected_tag" ]]' in prune_job


def test_all_actions_mirrors_use_90_day_retention_and_refresh_never_builds():
    publication = WORKFLOW.read_text(encoding='utf-8')
    refresh = REFRESH_WORKFLOW.read_text(encoding='utf-8')
    assert 'retention-days: 90' in publication
    assert 'retention-days: 90' in refresh
    assert 'gh release download "$ARTIFACT_NAME"' in refresh
    assert 'scripts/repository_bundle.py verify' in refresh
    assert 'scripts/repository_bundle.py build' not in refresh
    assert 'compression-level: 0' in refresh
    assert "-f context='Dish / repository bundle'" in refresh
    assert "description='Exact repository bundle mirror refreshed'" in refresh


def test_schedule_and_issue_recovery_are_separate_wrappers_over_same_refresh():
    schedule = SCHEDULE_WORKFLOW.read_text(encoding='utf-8')
    recovery = RECOVERY_WORKFLOW.read_text(encoding='utf-8')
    assert 'schedule:' in schedule
    assert 'issues:' not in schedule
    assert 'issues:' in recovery
    assert 'schedule:' not in recovery
    shared = 'uses: ./.github/workflows/repository-bundle-mirror-refresh.yml'
    assert shared in schedule
    assert shared in recovery
    assert 'repository-bundle mirror refresh' in recovery
    assert 'dish-repository-bundle-mirror-refresh:v1' in recovery
    assert '/collaborators/$ACTOR/permission' in recovery


def _refresh_event(sha: str, *, actor: str = 'marco', title: str = 'repository-bundle mirror refresh', body: str | None = None):
    return {
        'action': 'opened',
        'sender': {'login': actor},
        'issue': {
            'number': 42,
            'title': title,
            'body': body if body is not None else f'<!-- dish-repository-bundle-mirror-refresh:v1 sha={sha} -->',
            'user': {'login': actor},
        },
    }


def _run_refresh_request(tmp_path: Path, event: dict, sha: str, permission: str = 'write'):
    event_path = tmp_path / 'event.json'
    event_path.write_text(json.dumps(event), encoding='utf-8')
    return run(
        sys.executable, str(REFRESH_REQUEST_SCRIPT),
        '--event-path', str(event_path), '--permission', permission,
        '--current-main-sha', sha, check=False,
    )


def test_issue_refresh_request_accepts_only_exact_current_sha_and_trusted_writer(tmp_path: Path):
    sha = '1' * 40
    accepted = _run_refresh_request(tmp_path, _refresh_event(sha), sha)
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout) == {'actor': 'marco', 'issue_number': '42', 'source_sha': sha}

    stale = _run_refresh_request(tmp_path, _refresh_event('2' * 40), sha)
    assert stale.returncode == 2
    assert 'stale or mismatched' in stale.stderr

    untrusted = _run_refresh_request(tmp_path, _refresh_event(sha), sha, permission='read')
    assert untrusted.returncode == 2
    assert 'lacks repository write authority' in untrusted.stderr


def test_issue_refresh_request_rejects_malformed_or_impersonated_requests(tmp_path: Path):
    sha = '1' * 40
    malformed = _refresh_event(sha, body=f'please refresh\n<!-- dish-repository-bundle-mirror-refresh:v1 sha={sha} -->')
    result = _run_refresh_request(tmp_path, malformed, sha)
    assert result.returncode == 2
    assert 'body must contain only the exact v1 marker' in result.stderr

    impersonated = _refresh_event(sha)
    impersonated['sender']['login'] = 'someone-else'
    result = _run_refresh_request(tmp_path, impersonated, sha)
    assert result.returncode == 2
    assert 'opener/sender identity mismatch' in result.stderr


def test_same_sha_mirror_refresh_preserves_existing_release_bytes(tmp_path: Path):
    source, _remote, sha = make_repo(tmp_path)
    output, built = build(tmp_path, source, sha)
    names = [built['bundle_name'], built['manifest_name'], built['checksum_name']]
    durable = {name: (output / name).read_bytes() for name in names}

    copied = tmp_path / 'copied-release-bytes'
    copied.mkdir()
    for name, data in durable.items():
        (copied / name).write_bytes(data)
    completed = verify(tmp_path, copied, sha, clone=tmp_path / 'refresh-clone')
    assert completed.returncode == 0, completed.stderr
    assert {name: (copied / name).read_bytes() for name in names} == durable

    # Rebuilding the same SHA under a different run changes manifest provenance,
    # so refresh must copy the immutable Release bytes rather than call build.
    rebuilt = tmp_path / 'rebuilt'
    run(
        sys.executable, str(SCRIPT), 'build', '--repo-root', str(source), '--output-dir', str(rebuilt),
        '--repository', REPOSITORY, '--repository-id', REPOSITORY_ID,
        '--source-sha', sha, '--source-ref', MAIN_REF, '--event-name', 'push',
        '--event-ref', MAIN_REF, '--event-sha', sha, '--workflow', 'Repository bundle publication',
        '--workflow-ref', 'workflow@main', '--workflow-sha', sha, '--run-id', '999', '--run-attempt', '2',
    )
    assert (rebuilt / built['manifest_name']).read_bytes() != durable[built['manifest_name']]

def test_build_verify_clone_round_trip(tmp_path: Path):
    source, _remote, sha = make_repo(tmp_path)
    output, built = build(tmp_path, source, sha)
    assert built['artifact_name'] == f'repository-bundle-{sha}'
    manifest = json.loads((output / built['manifest_name']).read_text(encoding='utf-8'))
    assert manifest['repository'] == {'full_name': REPOSITORY, 'id': REPOSITORY_ID}
    assert manifest['source'] == {'sha': sha, 'ref': MAIN_REF}
    assert manifest['generator']['event'] == {'name': 'push', 'sha': sha, 'ref': MAIN_REF}
    assert manifest['advertised_refs'] == [{'sha': sha, 'ref': MAIN_REF}]

    completed = verify(tmp_path, output, sha)
    assert completed.returncode == 0, completed.stderr
    verified = json.loads(completed.stdout)
    assert verified['cloned_head'] == sha
    assert git(tmp_path / 'clone', 'rev-parse', 'refs/remotes/origin/main') == sha
    assert git(tmp_path / 'clone', 'remote', 'get-url', 'origin') == f'https://github.com/{REPOSITORY}.git'
    assert git(tmp_path / 'clone', 'log', '--oneline', '-1').endswith(' initial')


def test_build_allows_pushed_main_event_to_finish_after_remote_main_advances(tmp_path: Path):
    source, _remote, old_sha = make_repo(tmp_path)
    (source / 'hello.txt').write_text('new pushed main\n', encoding='utf-8')
    git(source, 'add', 'hello.txt')
    git(source, 'commit', '-m', 'advance main')
    new_sha = git(source, 'rev-parse', 'HEAD')
    git(source, 'push', 'origin', 'main')
    assert new_sha != old_sha
    assert git(source, 'ls-remote', '--exit-code', 'origin', MAIN_REF).split()[0] == new_sha

    # Model the older workflow run after a later main push: the event commit still exists,
    # and the workflow deliberately materializes its own exact advertised main ref.
    git(source, 'update-ref', MAIN_REF, old_sha)
    output, built = build(tmp_path, source, old_sha)
    assert built['source_sha'] == old_sha
    completed = verify(tmp_path, output, old_sha)
    assert completed.returncode == 0, completed.stderr


def test_build_rejects_workflow_event_sha_mismatch(tmp_path: Path):
    source, _remote, sha = make_repo(tmp_path)
    other_sha = '0' * 40
    completed = run(
        sys.executable, str(SCRIPT), 'build', '--repo-root', str(source),
        '--output-dir', str(tmp_path / 'bundle'), '--repository', REPOSITORY,
        '--repository-id', REPOSITORY_ID, '--source-sha', sha,
        '--source-ref', MAIN_REF, '--event-name', 'push', '--event-ref', MAIN_REF,
        '--event-sha', other_sha, '--workflow', 'test', '--workflow-ref', 'test@main',
        '--workflow-sha', sha, '--run-id', '1', '--run-attempt', '1', check=False,
    )
    assert completed.returncode == 2
    assert 'workflow event SHA mismatch' in completed.stderr


def test_authority_command_still_rejects_stale_current_main_sha(tmp_path: Path):
    source, _remote, old_sha = make_repo(tmp_path)
    (source / 'hello.txt').write_text('new current main\n', encoding='utf-8')
    git(source, 'add', 'hello.txt')
    git(source, 'commit', '-m', 'advance main')
    git(source, 'push', 'origin', 'main')
    completed = run(
        sys.executable, str(SCRIPT), 'authority', '--repo-root', str(source),
        '--source-sha', old_sha, '--source-ref', MAIN_REF, check=False,
    )
    assert completed.returncode == 2
    assert 'stale or mismatched' in completed.stderr


def test_verify_rejects_checksum_tampering(tmp_path: Path):
    source, _remote, sha = make_repo(tmp_path)
    output, built = build(tmp_path, source, sha)
    bundle = output / built['bundle_name']
    bundle.write_bytes(bundle.read_bytes() + b'tamper')
    completed = verify(tmp_path, output, sha)
    assert completed.returncode == 2
    assert 'bundle checksum mismatch' in completed.stderr


def test_verify_rejects_repository_identity_mismatch(tmp_path: Path):
    source, _remote, sha = make_repo(tmp_path)
    output, _built = build(tmp_path, source, sha)
    completed = verify(tmp_path, output, sha, repository='someone/else')
    assert completed.returncode == 2
    assert 'repository identity mismatch' in completed.stderr


def test_verify_rejects_manifest_advertised_ref_mismatch(tmp_path: Path):
    source, _remote, sha = make_repo(tmp_path)
    output, built = build(tmp_path, source, sha)
    manifest_path = output / built['manifest_name']
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['advertised_refs'] = []
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    completed = verify(tmp_path, output, sha)
    assert completed.returncode == 2
    assert 'advertised refs differ from manifest' in completed.stderr


def test_v1_rejects_non_main_ref(tmp_path: Path):
    source, _remote, sha = make_repo(tmp_path)
    completed = run(
        sys.executable, str(SCRIPT), 'authority', '--repo-root', str(source),
        '--source-sha', sha, '--source-ref', 'refs/heads/feature', check=False,
    )
    assert completed.returncode == 2
    assert 'support only refs/heads/main' in completed.stderr
