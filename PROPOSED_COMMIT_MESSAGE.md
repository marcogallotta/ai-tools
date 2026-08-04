Stabilize the PGlite development harness

PGlite development tests intermittently failed after the embedded TCP launcher
reported readiness. The launcher used a raw socket accept as its readiness
signal and exposed a one-client connection limit, so rapid Alembic reconnects
could race prior-client cleanup and terminate or reject the next connection.
The report classifier also treated every JUnit error and any message containing
"pglite" or "OperationalError" as infrastructure, which could hide deterministic
assertion failures.

Replace raw-TCP readiness with the launcher's database-ready signal followed by
two independent PostgreSQL `SELECT 1, version()` handshakes. Generate the socket
server with a bounded sixteen-connection capacity and retry only recognized
startup lifecycle failures with a fresh directory and port. Once the fixture
yields, never intercept or rerun test-body exceptions.

Narrow report classification to explicit connection/startup signatures and add
a regression proving an assertion represented as a JUnit error remains an
assertion failure. Add direct unit contracts for connection capacity, SQL
readiness, startup-only retry, and non-retry of test failures.

Retain the populated PGlite migration correction already present in the v131
base and remove its obsolete target-revision import. Apply the identical
upgrade-to-head correction to the SQLite populated migration test.

Verification on this rebased package:
- Python compilation/import syntax checks passed for every changed Python file.
- The package was applied to a fresh v131 extraction and all payload files
  matched the staged tree byte-for-byte.
- Full pytest execution was unavailable in the packaging environment because
  requirements-test.txt could not obtain asana==5.2.5, while the uploaded
  Python 3.12 site-packages are binary-incompatible with the available Python
  3.13 interpreter.

No production behavior or native PostgreSQL certification contract changes.
