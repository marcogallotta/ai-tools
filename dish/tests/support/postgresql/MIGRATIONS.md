# Populated-predecessor migration tests

`MigrationDatabase` owns isolation, reset, Alembic configuration, revision setup/stamping, dialect assertions, upgrades, expected-failure handling, independent connections, and evidence classification.

Agent A should keep each migration's predecessor schema/rows and transformed-data assertions in a small migration-specific support module. A typical test is:

```python
database.initialize("<down_revision>")
database.seed(seed_valid_rows)
database.upgrade("<new_revision>")
database.assert_revision("<new_revision>")
database.read(assert_backfill)
database.seed(assert_constraints_reject_invalid_rows)
```

For a hand-built historical schema, call `database.reset()`, create only the predecessor objects, and then `database.stamp("<down_revision>")`. For intentionally conflicting rows, seed them and use `database.expect_upgrade_failure(...)` with the exact exception/message contract.

Use `native_migration_database` for certification, `pglite_migration_database` only for PostgreSQL-semantic development evidence, and `sqlite_migration_database` only for compatibility. Do not mark PGlite or SQLite results as native PostgreSQL certification. New native migration tests must also be added literally to `NATIVE_POSTGRESQL_CERTIFICATION_INVENTORY`.
