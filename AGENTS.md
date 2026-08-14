# Repository agent map

Read `CLAUDE.md` in this directory, then use `dish/docs/architecture/index.md` for any work under `dish/`. The index routes changes to the owning code, invariants, transaction boundaries, and proving tests; do not treat this file as an architecture encyclopedia.

For genuine Dish work, production is the default. Use test only for experiments, rehearsals, destructive testing, or Marco's explicit request. Before an ambiguous mutation, confirm the target. Never use production `dish-admin` or change the public Action route without Marco's explicit authorization.

For local Claude Code/Codex only, if post-compaction role/task/PR context is clearly incomplete or stale, treat pre-compaction history as `UNKNOWN` unless durable state or the local transcript verifies it, and do not resume substantive work until the repository hook re-grounding barrier has restored current authority. This is a fallback instruction; the hook is the primary mechanism.
