# Repository agent map

Read `CLAUDE.md` in this directory, then use `dish/docs/architecture/index.md` for any work under `dish/`. The index routes changes to the owning code, invariants, transaction boundaries, and proving tests; do not treat this file as an architecture encyclopedia.

For genuine Dish work, production is the default. Use test only for experiments, rehearsals, destructive testing, or Marco's explicit request. Before an ambiguous mutation, confirm the target. Never use production `dish-admin` or change the public Action route without Marco's explicit authorization.
