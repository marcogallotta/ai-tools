import assert from "node:assert/strict";
import test from "node:test";
import { installFixtureReviewBoundary } from "../../src/js/review/review-boundary.js";

function fakeTarget() {
  const calls = [];
  return {
    calls,
    location: { href: "http://review.test/", origin: "http://review.test" },
    document: { documentElement: { dataset: {} } },
    fetch: async (input) => { calls.push(input); return { ok: true }; },
  };
}

test("fixture boundary allows local static reads and blocks backend access", async () => {
  const target = fakeTarget();
  installFixtureReviewBoundary(target);
  await target.fetch("/build.json");
  assert.equal(target.calls.length, 1);
  await assert.rejects(target.fetch("/api/board"), /blocks backend/);
  await assert.rejects(target.fetch("https://example.test/data"), /blocks backend/);
});
