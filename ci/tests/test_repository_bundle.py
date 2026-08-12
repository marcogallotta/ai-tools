from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'repository_bundle.py'
WORKFLOW = Path(__file__).resolve().parents[2] / '.github' / 'workflows' / 'repository-bundle.yml'
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


def test_pull_request_validation_has_read_only_contents_permission():
    workflow = WORKFLOW.read_text(encoding='utf-8')
    assert 'permissions:\n  contents: read\n' in workflow
    assert workflow.count('contents: write') == 1

    build_job = workflow.split('  build-verify-artifact:\n', 1)[1].split(
        '  publish-release-retention:\n', 1
    )[0]
    assert 'contents: write' not in build_job

    publish_job = workflow.split('  publish-release-retention:\n', 1)[1]
    assert "if: github.event_name != 'pull_request' && github.ref == 'refs/heads/main'" in publish_job
    assert 'permissions:\n      actions: read\n      contents: write\n' in publish_job


def test_build_verify_clone_round_trip(tmp_path: Path):
    source, _remote, sha = make_repo(tmp_path)
    output, built = build(tmp_path, source, sha)
    assert built['artifact_name'] == f'repository-bundle-{sha}'
    manifest = json.loads((output / built['manifest_name']).read_text(encoding='utf-8'))
    assert manifest['repository'] == {'full_name': REPOSITORY, 'id': REPOSITORY_ID}
    assert manifest['source'] == {'sha': sha, 'ref': MAIN_REF}
    assert manifest['advertised_refs'] == [{'sha': sha, 'ref': MAIN_REF}]

    completed = verify(tmp_path, output, sha)
    assert completed.returncode == 0, completed.stderr
    verified = json.loads(completed.stdout)
    assert verified['cloned_head'] == sha
    assert git(tmp_path / 'clone', 'rev-parse', 'refs/remotes/origin/main') == sha
    assert git(tmp_path / 'clone', 'remote', 'get-url', 'origin') == f'https://github.com/{REPOSITORY}.git'
    assert git(tmp_path / 'clone', 'log', '--oneline', '-1').endswith(' initial')


def test_build_fails_closed_when_requested_sha_is_not_current_remote_main(tmp_path: Path):
    source, _remote, old_sha = make_repo(tmp_path)
    (source / 'hello.txt').write_text('new local commit\n', encoding='utf-8')
    git(source, 'add', 'hello.txt')
    git(source, 'commit', '-m', 'not pushed')
    new_sha = git(source, 'rev-parse', 'HEAD')
    assert new_sha != old_sha
    completed = run(
        sys.executable, str(SCRIPT), 'build', '--repo-root', str(source),
        '--output-dir', str(tmp_path / 'bundle'), '--repository', REPOSITORY,
        '--repository-id', REPOSITORY_ID, '--source-sha', new_sha,
        '--source-ref', MAIN_REF, '--workflow', 'test', '--workflow-ref', 'test@main',
        '--workflow-sha', new_sha, '--run-id', '1', '--run-attempt', '1', check=False,
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
