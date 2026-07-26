# Dish tool — implemented local contract through Step 10

`dish` is the sole supported agent interface for protocol-managed Cooking tasks. The exact live Asana task is content authority; the local database stores operation, exact-content, Verification-cycle, audit, and recovery facts without replacing protocol state or agent judgment.

The implementation provides:

- exact Honest protocol/schema compatibility checks;
- deterministic Planning and canonical-task parsing/rendering;
- task-scoped exact-content identity and drift detection;
- one open operation per task in local single-agent test mode;
- stage-isolated Planning, Research, and Verification protocol delivery;
- confirmed full-state writes and independently confirmed movement;
- exact-cycle Verification releases and identity/attestation independence;
- Small, Large, Evidence, Human Review, and two-pass routes;
- destination-nonblocking readiness and movement-only submission;
- explicit older-schema migration and evidence-based uncertain recovery.

The canonical command syntax, JSON result envelope, exit statuses, rerun rules, and troubleshooting are in `dish-tool-activation.md`. Agent protocols contain only mandatory boundary hooks and semantic responsibilities. The ChatGPT relay contains only relay-specific constraints.

## Safety boundary

Steps 1–10 are for controlled local testing with one active agent at a time. They do not provide cross-checkout or multi-agent locking. Step 11 must place locks, shared state, credentials, and all Asana access behind one shared service before live multi-agent use.

## Invariants

- Every governed read, write, correction, signoff, and movement uses the tool.
- The complete live title and notes are reread before mutation and after every mutation.
- CRLF/LF is the only content-identity normalization.
- Tool operation state never implies protocol readiness.
- A tool pass never authorizes substantive handoff or signoff.
- The governing protocol wins over a tool/schema disagreement.
- Tool failures are tooling failures, not dish Evidence/Human blockers.
- Confirmed content write, signoff, and movement are separate idempotent completion facts.
- A material edit after signoff requires a new Verification cycle.
- Destination defects block final movement, not Research, Verification, or `ready`.
