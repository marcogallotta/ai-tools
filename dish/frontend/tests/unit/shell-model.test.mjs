import test from "node:test";
import assert from "node:assert/strict";
import { applicationShellModel, loginShellModel } from "../../src/js/shell/shell-model.js";

test("login shell exposes the private access model", () => {
  const model = loginShellModel();
  assert.equal(model.kind, "login");
  assert.match(model.description, /shared password/i);
});

test("protected shell is intentionally empty in Stage 0", () => {
  const model = applicationShellModel();
  assert.equal(model.kind, "protected-empty");
  assert.match(model.emptyDescription, /intentionally absent/i);
});
