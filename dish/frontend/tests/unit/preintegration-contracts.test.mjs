import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const contract = JSON.parse(await readFile(new URL("../../contracts/stage2-security-contract.json", import.meta.url)));
const openapi = JSON.parse(await readFile(new URL("../../openapi/frontend.openapi.json", import.meta.url)));

test("Stage 2 security contract remains closed and blocked", () => {
  assert.equal(contract.contract_version, openapi.info.version);
  assert.equal(contract.gate, "A");
  assert.equal(contract.status, "design-closed-implementation-blocked");
  assert.deepEqual(Object.keys(contract.blockers), Array.from({ length: 12 }, (_, index) => `A-${String(index + 1).padStart(2, "0")}`));
  assert.ok(Object.values(contract.blockers).every((value) => !value.includes("resolved")));
});

test("password and session bounds agree with frontend OpenAPI", () => {
  const schemas = openapi.components.schemas;
  assert.equal(schemas.LoginRequest.properties.password.maxLength, contract.password_policy.maximum_characters);
  assert.equal(schemas.Session.properties.remaining_seconds.maximum, contract.session_policy.lifetime_seconds);
  assert.equal(schemas.Session.properties.csrf_proof.minLength, 22);
  assert.equal(contract.password_policy.minimum_characters, 16);
  assert.equal(contract.password_policy.counting_rule, "unicode-code-points");
});

test("Stage 2 route contract covers exactly the current frontend API paths", () => {
  const apiRoutes = new Set([
    ...contract.route_families.protected_api,
    ...contract.route_families.lifecycle,
    ...contract.route_families.unauthenticated,
  ].filter((route) => route.includes("/frontend/") && !route.includes("/frontend/assets/")).map((route) => route.split(" ")[1]));
  const expected = new Set(Object.keys(openapi.paths));
  assert.deepEqual([...apiRoutes].sort(), [...expected].sort());
});

test("initial authority policy does not trust forwarded identity", () => {
  assert.equal(contract.authority_policy.required_scheme, "https");
  assert.equal(contract.authority_policy.forwarded_authority_headers, "ignored");
  assert.equal(contract.authority_policy.forwarded_client_address_headers, "ignored");
  assert.equal(contract.authority_policy.cors, "disabled");
});
