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

QUALIFY_FILE_TRANSPORT_CLIENT_ID = "implementation-action"
"""Only this action_client_id may successfully call qualify-file-transport.

The command stays listed and discoverable for every Action deployment (OpenAPI,
replay contract, PostgreSQL parity), but the Implementation Action Gate A
pilot is the only intended caller. A deployment configured with any other
DISH_ACTION_CLIENT_ID value is rejected at the command boundary rather than
being silently exposed to the default connected GPT.
"""

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
