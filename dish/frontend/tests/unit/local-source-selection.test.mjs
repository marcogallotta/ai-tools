import assert from "node:assert/strict";
import test from "node:test";
import { frontendDataSource } from "../../src/js/local/source-selection.js";

test("local PostgreSQL observation mode accepts one explicit PostgreSQL source", () => {
  assert.equal(frontendDataSource("?source=postgresql"), "postgresql");
});

test("local PostgreSQL observation mode rejects absent, unknown, or ambiguous sources", () => {
  assert.equal(frontendDataSource(""), null);
  assert.equal(frontendDataSource("?source=other"), null);
  assert.equal(frontendDataSource("?source=postgresql&source=postgresql"), null);
  assert.equal(frontendDataSource("?source=postgresql&source=fixture"), null);
});
