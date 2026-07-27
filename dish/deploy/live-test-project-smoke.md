# Optional Step 12 live test-project smoke

Do not run this against production Cooking. Use a disposable task in the configured test project and preserve the complete JSON transcript.

## Preconditions

- Step 11 unit and hermetic SDK tests pass.
- Service host uses `DISH_HONEST_PATH=/home/marco/honest-pantry-dish-rollout`.
- Service host uses the test `DISH_COOKING_PROJECT_GID=1216693403164366`.
- Test state is isolated under `/home/marco/.local/state/dish/test/`; it does not reuse the
  production database or backup directory.
- Private Serve and public Funnel endpoints match `deploy/tailscale/README.md`.
- The service database and Asana test project have been backed up.
- `DISH_LIVE_MODE=1` and `DISH_MODE=service` are set on CLI/admin clients.

## Checks

1. `GET /health` over the private endpoint is healthy.
2. The public endpoint returns 404 for `/health`, `/v1/commands/sections`, `/v1/admin/recover`, and `/v1/admin/backups/create`.
3. The Action token succeeds only on `/v1/action/sections`; CLI and admin tokens fail there.
4. Create one disposable task through `dish create` and confirm Research Queue placement.
5. Run Planning → Research Queue.
6. Run Research → Verification Queue using an exact candidate file.
7. Start a genuinely independent Verification run, approve, and submit to the configured non-queue destination.
8. Confirm title/notes identities and section membership after every write or movement.
9. Deliberately attempt a stale content baseline and stale placement baseline; assert zero mutation.
10. Simulate an expired client lease and use `dish-admin recover-lease` before recovery.
11. Create a managed backup, complete another harmless test operation, restore the backup, and confirm the prior operation/lease state returns exactly.
12. Delete the disposable Asana tasks only through the approved test cleanup path.

## Stop conditions

Stop immediately on any raw exception, `BACKEND_UNCERTAIN`, credential appearing in output, public access to a private route, duplicate provenance, repeated movement, or mismatch between live Asana state and local durable evidence.
