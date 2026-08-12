import { adminEmptyFixture, adminExtremeFixture, adminFixture } from "../../../fixtures/stage1-admin.js";
import { mapAdminResponse } from "../features/admin/admin-model.js";
import { renderAdmin } from "../features/admin/admin.js";
import { installFixtureReviewBoundary } from "../review/review-boundary.js";
import { createReviewToolbar } from "../review/review-toolbar.js";
import { createApplicationFrame } from "../shell/application-shell.js";

export function adminFixtureForScenario(name) {
  if (name === "admin-empty") return structuredClone(adminEmptyFixture);
  if (name === "admin-extreme") return structuredClone(adminExtremeFixture);
  return structuredClone(adminFixture);
}

export function renderFixtureAdmin(root, scenario = "admin", { reviewMode = false } = {}) {
  if (reviewMode) installFixtureReviewBoundary();
  const { shell, main } = createApplicationFrame({ environmentLabel: "Fixture prototype — not canonical data" });
  if (reviewMode) shell.querySelector(".app-header")?.after(createReviewToolbar(scenario));
  renderAdmin(main, mapAdminResponse(adminFixtureForScenario(scenario)));
  root.replaceChildren(shell);
  root.hidden = false;
  root.dataset.shellState = "fixture-admin";
  root.dataset.fixtureScenario = scenario;
}
