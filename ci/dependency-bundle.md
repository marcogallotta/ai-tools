# Dependency-bundle publication and discovery

Release assets under `dependency-bundle-<bundle-id>` are the immutable byte authority. Actions artifacts and the `Dish / dependency bundle` commit status are discovery mirrors only; they never replace or repair Release bytes.

For an ordinary change that produces a new expected bundle ID before landing, the author opens one issue titled `dependency-bundle candidate publication` with exactly this marker:

```text
<!-- dish-dependency-bundle-candidate:v1 task=<16-digit-task> pr=<number> head=<40-hex-head> bundle=<bundle-id> -->
```

The default-branch workflow validates the authorized writer, open same-repository PR, exact current head, changed compatibility inputs, and recomputed bundle ID. Trusted default-branch tooling treats the candidate as data, builds only binary wheels, publishes new immutable assets or byte-compares an existing Release, then dispatches the mirror for the exact candidate head. Malformed, stale, moved, closed, foreign, unauthorized, or mismatched requests fail closed.

The mirror writes a pending then successful `Dish / dependency bundle` status targeting its exact run, uploads exactly one live artifact named by bundle ID, and reads that inventory back. `scripts/dependency_bundle_locator.py` validates the unique status/run/artifact tuple before connector download. The downloaded archive, checksum, and manifest still require `scripts/dependency_bundle.py verify` or `install`; the manifest binds `built_from_commit` to the pre-land candidate head.

After landing, the main-push mirror recomputes the expected ID, mirrors the same immutable Release without rebuilding, and publishes the main commit locator. Acquisition starts from repository identity plus an exact freshly read main SHA, computes the expected ID, follows only the unique successful locator, verifies and installs offline, then rereads main. Main movement restarts discovery. Missing, stale, duplicate, expired, corrupt, wrong-runtime, or mismatched evidence is a typed capability failure, never a request for an uploaded virtual environment or human-supplied run/artifact ID.
