"""Stable connected-agent command identities shared below service composition.

This module owns names and exposure membership only. Workflow legality remains in
workflow policy, while transport principal, replay, routing, validation, and OpenAPI
metadata remain service concerns.
"""

CREATE = "create"
SECTIONS = "sections"
SECTION_TASKS = "section-tasks"
READ = "read"
PROPOSALS = "proposals"
APPLY_PROPOSAL = "apply-proposal"
SAFE_RECLAIM = "safe-reclaim"
INSPECT = "inspect"
START = "start"
PREPARE = "prepare"
APPROVE = "approve"
REJECT = "reject"
SUBMIT = "submit"
RENEW_LEASE = "renew-lease"
QUALIFY_FILE_TRANSPORT = "qualify-file-transport"

CONNECTED_AGENT_COMMANDS = (
    CREATE,
    SECTIONS,
    SECTION_TASKS,
    READ,
    PROPOSALS,
    APPLY_PROPOSAL,
    SAFE_RECLAIM,
    INSPECT,
    START,
    PREPARE,
    APPROVE,
    REJECT,
    SUBMIT,
    RENEW_LEASE,
    QUALIFY_FILE_TRANSPORT,
)

if len(set(CONNECTED_AGENT_COMMANDS)) != len(CONNECTED_AGENT_COMMANDS):
    raise ValueError("duplicate connected-agent command identity")
