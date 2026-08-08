import assert from "node:assert/strict";
import test from "node:test";
import { frontendDataSource } from "../../src/js/local/source-selection.js";

test("PostgreSQL mode is explicit and fixture mode remains the default", () => {
  assert.equal(frontendDataSource(""), "fixture");
  assert.equal(frontendDataSource("?source=postgresql"), "postgresql");
  assert.equal(frontendDataSource("?source=other"), "fixture");
  assert.equal(frontendDataSource("?source=postgresql&source=fixture"), "fixture");
});

test("review mode always preserves fixture isolation", () => {
  assert.equal(frontendDataSource("?review=1&source=postgresql"), "fixture");
  assert.equal(frontendDataSource("?review=0&review=1&source=postgresql"), "fixture");
});
