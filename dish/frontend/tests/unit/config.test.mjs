import assert from "node:assert/strict";
import test from "node:test";
import { activeRefreshIntervalMs } from "../../src/js/config.js";

function documentWith(value) {
  return { querySelector: () => value == null ? null : { content: value } };
}

test("active refresh interval is bounded to the approved 30-second ceiling", () => {
  assert.equal(activeRefreshIntervalMs(documentWith("25")), 25000);
  assert.equal(activeRefreshIntervalMs(documentWith("1")), 1000);
  assert.equal(activeRefreshIntervalMs(documentWith(null)), 25000);
  for (const value of ["0", "31", "1.5", "not-a-number"]) {
    assert.throws(() => activeRefreshIntervalMs(documentWith(value)), /refresh interval/);
  }
});
