# Global Implementation claim service runbook

This runbook operates the repository-owned cross-host writer-claim service used by Dish
Implementation/fix agents. The service is **orchestration fencing**, not code-review, Integration,
or workflow authority.

## Authority and topology

One service instance (or one active process group sharing the same local SQLite database) owns the
CAS store for the configured repository. All ChatGPT/Claude Code/Codex writable Implementation
clients for that repository must reach that same HTTPS service. Do not copy the SQLite database to
multiple writable hosts or run independent claim services per agent host.

The durable key is `(repository, task_gid)`. A current row has an opaque public `claim_id` generation and
records owner/session/host provenance, authoring base, lifecycle state, branch/PR lineage, exact heads,
and Asana synchronization state. The service separately stores only a hash of a high-entropy
per-generation writer capability. The cleartext capability is returned only to the winning acquire or
explicitly authorized takeover caller; it is not part of status/conflict/dispatch responses and is never
mirrored to Asana. Asana receives the public generation marker for visibility and reconstruction;
GitHub branch/PR state is lineage/readback evidence. Neither is a second claim writer.

## Service process

The server entry point is:

```sh
tools/implementation-claim-server
```

Required environment:

- `DISH_IMPLEMENTATION_CLAIM_DB`: durable SQLite path on the service host;
- `DISH_IMPLEMENTATION_CLAIM_SERVICE_TOKEN`: ordinary bearer token accepted by the private service;
- `DISH_IMPLEMENTATION_CLAIM_RECOVERY_TOKEN`: separate recovery/orchestration bearer token, distinct from the ordinary service token;
- `DISH_IMPLEMENTATION_CLAIM_PROJECTS`: comma-separated Asana project GIDs allowed for claims;
- `ASANA_PAT`: Asana credential able to read the task/project and move/comment the task;
- `GITHUB_TOKEN`: read-only GitHub credential sufficient to resolve branch heads for recovery;
- optional `DISH_IMPLEMENTATION_CLAIM_REPOSITORY` (defaults to `marcogallotta/ai-tools`);
- optional bind/port (`DISH_IMPLEMENTATION_CLAIM_BIND`, `DISH_IMPLEMENTATION_CLAIM_PORT`).

Bind the Python service to a private/loopback interface and expose it to agent hosts only through an
authenticated TLS/private-network endpoint. Production clients reject plain HTTP. Do not expose the
bearer token in task comments, logs, PRs, or command output.

Client environment:

```text
DISH_IMPLEMENTATION_CLAIM_URL=https://<private-claim-endpoint>
DISH_IMPLEMENTATION_CLAIM_TOKEN=<service bearer token>
DISH_IMPLEMENTATION_CLAIM_REPOSITORY=marcogallotta/ai-tools
```

Ordinary writable agent hosts do **not** receive `DISH_IMPLEMENTATION_CLAIM_RECOVERY_TOKEN`. That
credential is provisioned only to the authorized recovery/orchestration path that may replace a current
generation. The winning writer keeps its generation capability in private local claim state (`0600`
under the `0700` Dish state directory) and the wrapper passes it to the claimed child process via
`DISH_IMPLEMENTATION_GLOBAL_WRITER_CAPABILITY`. Do not put the writer capability or recovery token in
Asana, PR text, status output, shell history, shared logs, or handoff prose.

The direct SQLite adapter (`DISH_IMPLEMENTATION_CLAIM_TEST_DB` plus
`DISH_IMPLEMENTATION_CLAIM_TESTING=1`) is repository-test-only and is rejected unless the explicit
test flag is set.

## Dispatch and acquisition

Before ordinary writable Implementation dispatch, query:

```sh
tools/implementation-claim dispatch-check --task <gid>
```

Only `dispatchable=true` permits a **fresh** acquisition. Any existing generation requires explicit
continuation or exact-generation takeover; a stale Asana `Ready` section is not an unlock. Service
unavailability, Asana synchronization/readback failure, or contradictory lineage fails closed.

Local Claude Code/Codex dispatch uses the canonical `tools/agent-worktree claim ... -- ...` wrapper.
That wrapper acquires/validates the global generation before branch/worktree mutation and layers the
same-host OS locks beneath it. Knowing only the public `claim_id` is insufficient to continue a live
generation: every writable service operation also verifies the private writer capability for that exact
generation.

## Takeover and recovery

Takeover always supplies the exact current `claim_id`, the unchanged authoring base, an explicit
handoff/recovery reason, bounded liveness evidence, **and the distinct recovery/orchestration authority**.
Time passage alone and possession of the ordinary service credential are not sufficient. Successful CAS
creates a fresh generation plus fresh writer capability and permanently fences both the old public id and
the old private writer capability. Concurrent authorized takeovers still race on the exact prior
generation CAS, so exactly one can win.

Database rows created by the earlier schema have no writer-capability hash. They fail closed for ordinary
writes after upgrade and require an explicitly authorized exact-generation takeover to mint a fresh writer
capability; do not synthesize or infer a capability for an old row.

If a publication journal entry is still `pending`, takeover/release/supersede is refused. First use
`reconcile-publication` against GitHub. If GitHub still equals the exact expected head and recovery has
established that no authorized push remains in flight, explicitly `abort-publication`; then perform
the exact-generation takeover. If GitHub equals the proposed head, reconciliation completes that
publication and Asana synchronization before takeover. Any third head is `HEAD_MOVED` and requires
lineage investigation.

## Publication boundary

Every publisher must call `begin-publication` with `task_gid + claim_id +` the private writer capability
`+ branch + expected_head + proposed_head + request_id` before moving GitHub. The store admits at most one unresolved intent for
the task lineage and makes the claim non-writable until the intent's exact Asana marker is read back.
After the branch CAS/push, call `complete-publication`. On ambiguous network/process failure, use
`reconcile-publication`; do not blindly retry the GitHub write.

The future connector-native expected-head publisher consumes this same API. A correct Git expected
head with the wrong/stale `claim_id`, or with the public current `claim_id` but without its private writer
capability, is authorization failure with zero branch mutation.

## Review-ready and terminal state

`review-ready` requires the exact bound PR/head and no pending publication. It removes writable
Implementation authority while retaining one durable lineage. A formal exact-head Review block may
hand the same lineage to a replacement fix owner through exact-generation takeover. Release and
supersede are exact-generation operations and do not erase history.

## Backup and recovery

The SQLite database is authority. Back it up from the service host with an SQLite-consistent snapshot
(`sqlite3 <db> '.backup <destination>'` or equivalent filesystem/application-consistent mechanism).
Do not restore an older copy over a running service. Stop writes, preserve the current database, and
compare the latest claim/event/publication records with Asana markers and GitHub branch/PR heads
before any restore. A restore that could regress a generation is an authority event and requires an
explicit recovery decision; never infer that an older generation becomes valid again.

## Rollout boundary

Landing this code does not itself deploy or activate the service and performs no TEST/PROD mutation.
Normal multi-host Implementation dispatch becomes collision-safe only when all writable agent
surfaces are provisioned to use the service. The connector-native publisher must require the same
claim before GitHub mutation; ordinary raw Git/ref mutation must not be retained as a normal fallback
once the governed publisher is available.
