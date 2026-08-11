import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  WORKFLOW_LABEL_MAX_LENGTH,
  WORKFLOW_OPERATION_LABELS,
  WORKFLOW_PHASE_LABELS,
} from "../../src/js/features/board/api-board-model.js";

const contract = JSON.parse(await readFile(new URL("../../contracts/stage2-security-contract.json", import.meta.url)));
const openapi = JSON.parse(await readFile(new URL("../../openapi/frontend.openapi.json", import.meta.url)));

test("Stage 2 security implementation candidate remains Gate A blocked", () => {
  assert.equal(contract.contract_version, openapi.info.version);
  assert.equal(contract.gate, "A");
  assert.equal(contract.status, "implementation-candidate-gate-a-not-passed");
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
  ].filter((route) => route.includes("/frontend/")).map((route) => route.split(" ")[1]));
  const expected = new Set(Object.keys(openapi.paths));
  assert.deepEqual([...apiRoutes].sort(), [...expected].sort());
});

test("initial authority policy does not trust forwarded identity", () => {
  assert.equal(contract.authority_policy.required_scheme, "https");
  assert.equal(contract.authority_policy.forwarded_authority_headers, "ignored");
  assert.equal(contract.authority_policy.forwarded_client_address_headers, "ignored");
  assert.equal(contract.authority_policy.cors, "disabled");
});

const stage3 = JSON.parse(await readFile(new URL("../../contracts/stage3-read-contract.json", import.meta.url)));
const migrationHead = await readFile(new URL("../../../dish_pg/migrations/versions/0037_release_identity_contract.py", import.meta.url), "utf8");
const modelSources = await Promise.all([
  "../../../dish_pg/models.py",
  "../../../dish_pg/stage3_models.py",
  "../../../dish_pg/stage5_models.py",
  "../../../dish_pg/stage6_models.py",
].map((path) => readFile(new URL(path, import.meta.url), "utf8")));
const allModels = modelSources.join("\n");

test("Stage 3 contract is reconciled to the checked-in migration head", () => {
  assert.match(migrationHead, /revision\s*=\s*["']0037_release_identity_contract["']/);
  assert.equal(stage3.checked_in_schema.alembic_head, "0037_release_identity_contract");
  assert.equal(stage3.checked_in_schema.production_status, "dark-launch-target-non-authoritative");
});

test("Stage 3 canonical source inventory names real current tables", () => {
  for (const source of Object.values(stage3.eligibility_inputs_present)) {
    const table = source.split(".")[0];
    assert.ok(allModels.includes(`__tablename__ = "${table}"`), `${table} must exist in current models`);
  }
});

test("WorkflowStatus OpenAPI and browser validation share the closed presentation registry", () => {
  const variants = openapi.components.schemas.WorkflowStatus.oneOf;
  const inactive = variants.find((item) => item.properties.state.enum.includes("no_active_operation"));
  const active = variants.find((item) => item.properties.state.enum.includes("active_operation"));
  assert.deepEqual(inactive.required, ["state"]);
  assert.deepEqual(active.required, ["state", "operation", "phase"]);
  assert.deepEqual(active.properties.operation.enum, [...WORKFLOW_OPERATION_LABELS]);
  assert.deepEqual(active.properties.phase.enum, [...WORKFLOW_PHASE_LABELS]);
  assert.equal(active.properties.operation.maxLength, WORKFLOW_LABEL_MAX_LENGTH);
  assert.equal(active.properties.phase.maxLength, WORKFLOW_LABEL_MAX_LENGTH);
});

test("frontend support-table inventory matches the current Stage 3 contract", async () => {
  const securityModels = await readFile(new URL("../../../dish_pg/frontend_security_models.py", import.meta.url), "utf8");
  for (const table of stage3.frontend_migration_requirements.stage2_present) {
    assert.ok(securityModels.includes(`__tablename__ = "${table}"`), `${table} must exist in frontend security models`);
  }
  for (const table of stage3.frontend_migration_requirements.stage3) {
    assert.ok(!allModels.includes(`__tablename__ = "${table}"`), `${table} unexpectedly exists; update the reconciliation contract`);
  }
  assert.equal(stage3.blockers["B-12"], "independent-review-pending");
});

const stage2Cases = JSON.parse(await readFile(new URL("../../contracts/stage2-acceptance-cases.json", import.meta.url)));
const stage3Cases = JSON.parse(await readFile(new URL("../../contracts/stage3-acceptance-cases.json", import.meta.url)));

function assertAcceptanceManifest(manifest, prefix, allowedBlockers) {
  assert.match(manifest.status, /(scaffold-only|implementation-candidate)/);
  assert.ok(manifest.cases.length >= 15);
  const ids = manifest.cases.map((item) => item.id);
  assert.equal(new Set(ids).size, ids.length);
  for (const item of manifest.cases) {
    assert.ok(item.id.startsWith(prefix));
    assert.ok(item.level.length > 0);
    assert.ok(item.assertion.length >= 40);
    assert.ok(item.blockers.length > 0);
    assert.ok(item.blockers.every((blocker) => allowedBlockers.has(blocker)));
  }
}

test("Stage 2 acceptance manifest covers only Gate A dependencies", () => {
  assertAcceptanceManifest(stage2Cases, "S2-", new Set(Object.keys(contract.blockers)));
});

test("Stage 3 acceptance scaffold covers only Gate B dependencies", () => {
  assertAcceptanceManifest(stage3Cases, "S3-", new Set(Object.keys(stage3.blockers)));
});
