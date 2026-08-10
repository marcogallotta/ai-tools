import test from "node:test";
import assert from "node:assert/strict";
import { isPrototypeApplicationView } from "../../src/js/prototype/prototype-routes.js";

test("only the explicit app query selects the protected shell preview", () => {
  assert.equal(isPrototypeApplicationView("?view=app"), true);
  assert.equal(isPrototypeApplicationView("?view=login"), false);
  assert.equal(isPrototypeApplicationView(""), false);
});
