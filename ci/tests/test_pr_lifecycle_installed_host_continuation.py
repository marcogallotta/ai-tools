from __future__ import annotations

from copy import deepcopy

import test_pr_lifecycle as base
import installed_host_cert

p = base.pr_lifecycle


class RecordingFixRouter:
    def __init__(self, *, chatgpt=True, local=True):
        self.commands = {
            "CHATGPT_IMPLEMENTATION": "chatgpt" if chatgpt else None,
            "LOCAL_IMPLEMENTATION": "local" if local else None,
        }
        self.calls = []

    @property
    def command(self):
        return next((value for value in self.commands.values() if value), None)

    def command_for(self, host):
        return self.commands.get(host)

    def dispatch(self, context, *, host):
        self.calls.append((host, deepcopy(context)))
        context_head = context["head"]
        assert context_head == base.HEAD


def _hook_files():
    return [{"filename": "hooks/agent-reground", "status": "modified", "patch": "@@ -1 +1 @@"}]


def _certificate(*, head=base.HEAD):
    task = "1217443403986570"
    digest = "d" * 64
    host_result = lambda host: {
        "host": host,
        "version": f"{host} 1.2.3",
        "binary": f"/usr/local/bin/{host}",
        "effective_config_sources": [f"/home/marco/.{host}/effective-config"],
        "active_paths": [
            {
                "path": "/home/marco/.local/bin/agent-reground",
                "resolved_target": "/worktree/hooks/agent-reground",
                "sha256": digest,
            }
        ],
        "loader_execution": {"actual_installed_binary": True, "result": "pass"},
        "harmless_governed_action": "pass",
        "deliberate_conflict": "denied",
    }
    return {
        "schema": installed_host_cert.SCHEMA,
        "repository": "marcogallotta/ai-tools",
        "pr_number": 31,
        "branch": "agent/test",
        "head": head,
        "task_ids": [task],
        "required_hosts": ["claude", "codex"],
        "changed_paths": ["hooks/agent-reground"],
        "identity": {
            "agent_id": "local-session-1",
            "host": "codex",
            "source": "launch-provenance",
            "claim_id": "claim-generation-123456",
            "launch_id": "launch-generation-123456",
            "task_gid": task,
            "branch": "agent/test",
            "pr_number": 31,
            "pr_head": head,
        },
        "fence": {
            "window": "full",
            "mechanism": "exclusive-shared-host-fence",
            "fence_id": "host-cert-window-123456",
            "producer_classes": ["claude", "codex", "host-config-writer"],
            "pre_state_digest": digest,
            "final_state_digest": digest,
            "concurrent_change_detected": False,
            "started_at": "2026-08-17T18:00:00Z",
            "ended_at": "2026-08-17T18:05:00Z",
        },
        "hosts": [host_result("claude"), host_result("codex")],
        "checks": {
            "unidentified_session_fails_closed": True,
            "compaction_recovery": True,
            "broken_asana_recovery": True,
            "worktree_prerequisites": True,
            "shell_config_trust": True,
            "no_stale_removed_references": True,
            "effective_config_parity": True,
            "head_movement_invalidation": True,
            "security_decision_boundary": True,
        },
        "disposition": {
            "mode": "temporary-restored",
            "readback": "pass",
            "readback_digest": digest,
        },
    }


def _certificate_comment(*, head=base.HEAD, comment_id=80):
    return {
        "id": comment_id,
        "body": installed_host_cert.render_comment(_certificate(head=head)),
        "created_at": base.NOW.isoformat(),
        "updated_at": base.NOW.isoformat(),
    }


def test_hook_change_without_exact_head_host_certificate_is_pre_review_implementation_continuation():
    gh = base.FakeGitHub(base.pr(draft=False))
    gh.pr_files = _hook_files()

    state = base.engine(gh).inspect(gh.pr)

    assert state.state == p.LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED
    assert state.authoring_evidence == installed_host_cert.EVIDENCE
    assert state.human_action is None
    assert "installed-host" in (state.residual_reason or "")


def test_non_host_change_does_not_create_installed_host_gate():
    gh = base.FakeGitHub(base.pr(draft=False))
    gh.pr_files = [{"filename": "dish/docs/example.md", "status": "modified", "patch": "docs"}]
    assert base.engine(gh).inspect(gh.pr).state == p.LifecycleState.REVIEW_READY


def test_valid_exact_head_host_certificate_allows_review_ready_transition():
    gh = base.FakeGitHub(base.pr(draft=False))
    gh.pr_files = _hook_files()
    gh.comments = [_certificate_comment()]

    state = base.engine(gh).inspect(gh.pr)

    assert state.state == p.LifecycleState.REVIEW_READY


def test_head_movement_invalidates_prior_host_certificate():
    gh = base.FakeGitHub(base.pr(head=base.NEW_HEAD, draft=False))
    gh.pr_files = _hook_files()
    gh.comments = [_certificate_comment(head=base.HEAD)]

    state = base.engine(gh).inspect(gh.pr)

    assert state.state == p.LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED
    assert state.head == base.NEW_HEAD
    assert "no exact-head" in (state.residual_reason or "")


def test_hook_host_continuation_selects_local_worker_and_passes_exact_requirements():
    gh = base.FakeGitHub(base.pr(draft=True))
    gh.pr_files = _hook_files()
    fixer = RecordingFixRouter()
    lifecycle = base.engine(gh)

    result = lifecycle.dispatch_one(
        lifecycle.inspect(gh.pr), workspace=None, local_reviewer=None, implementation_fixer=fixer
    )

    assert len(fixer.calls) == 1
    host, context = fixer.calls[0]
    assert host == "LOCAL_IMPLEMENTATION"
    assert context["implementation_host"] == "LOCAL_IMPLEMENTATION"
    cert = context["installed_host_certification"]
    assert cert["required_hosts"] == ["claude", "codex"]
    assert cert["changed_paths"] == ["hooks/agent-reground"]
    assert "--require-launch-provenance" in cert["identity"]["claim"]
    assert "Marco is not the tester or installer" in context["instruction"]
    assert any("consequential decision" in item for item in cert["acceptance"])
    assert result.state == p.LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED
    assert any(lease["phase"] == "implementation" for lease in result.active_leases)
    handoff = next(body for kind, body in gh.events if kind == "comment" and "dish-implementation-continuation:v1" in body)
    assert "pre-Review LOCAL IMPLEMENTATION continuation" in handoff
    assert "actual installed-loader/tool execution" in handoff


def test_newer_malformed_exact_head_certificate_invalidates_older_pass():
    gh = base.FakeGitHub(base.pr(draft=False))
    gh.pr_files = _hook_files()
    valid = _certificate_comment(comment_id=80)
    malformed = {
        "id": 81,
        "body": f"<!-- {installed_host_cert.MARKER} head={base.HEAD} result=pass hosts=claude,codex digest=not-a-digest -->\nINSTALLED HOST CERTIFICATE",
        "created_at": "2026-08-17T19:00:00+00:00",
        "updated_at": "2026-08-17T19:00:00+00:00",
    }
    gh.comments = [valid, malformed]

    state = base.engine(gh).inspect(gh.pr)

    assert state.state == p.LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED
    assert "malformed" in (state.residual_reason or "")


def test_certificate_missing_security_decision_boundary_fails_closed():
    gh = base.FakeGitHub(base.pr(draft=False))
    gh.pr_files = _hook_files()
    certificate = _certificate()
    certificate["checks"].pop("security_decision_boundary")
    body = installed_host_cert.render_comment(certificate)
    gh.comments = [{"id": 90, "body": body, "created_at": base.NOW.isoformat(), "updated_at": base.NOW.isoformat()}]

    state = base.engine(gh).inspect(gh.pr)

    assert state.state == p.LifecycleState.IMPLEMENTATION_CONTINUATION_REQUIRED
    assert "security_decision_boundary" in (state.residual_reason or "")
