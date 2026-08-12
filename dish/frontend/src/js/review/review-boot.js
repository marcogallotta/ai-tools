import { adminEmptyFixture, adminExtremeFixture, adminFixture } from "../../../fixtures/stage1-states.js";
import { DOCUMENT_TITLE } from "../config.js";
import { mapAdminResponse } from "../features/admin/admin-model.js";
import { renderAdmin } from "../features/admin/admin.js";
import { renderFixturePrototype } from "../prototype/prototype-app.js";
import { parseTaskRoute } from "../prototype/prototype-routes.js";
import { createApplicationFrame } from "../shell/application-shell.js";
import { renderLoginShell } from "../shell/login-shell.js";
import { installFixtureReviewBoundary } from "./review-boundary.js";
import { isAdminReviewScenario, isReviewScenario, scenarioTaskId } from "./review-catalog.js";
import { createReviewToolbar } from "./review-toolbar.js";

function adminFixtureForScenario(name) {
  if (name === "admin-empty") return structuredClone(adminEmptyFixture);
  if (name === "admin-extreme") return structuredClone(adminExtremeFixture);
  return structuredClone(adminFixture);
}

function renderFixtureAdmin(root, scenario) {
  const { shell, main } = createApplicationFrame({ environmentLabel: "Fixture prototype — not canonical data" });
  shell.querySelector(".app-header")?.after(createReviewToolbar(scenario));
  renderAdmin(main, mapAdminResponse(adminFixtureForScenario(scenario)));
  root.replaceChildren(shell);
  root.hidden = false;
  root.dataset.shellState = "fixture-admin";
  root.dataset.fixtureScenario = scenario;
}

export function resolveReviewInitialView(search = window.location.search, pathname = window.location.pathname) {
  const parameters = new URLSearchParams(search);
  const requestedScenario = parameters.get("scenario") ?? "board";
  const view = parameters.get("view") === "login" ? "login" : "app";
  const scenario = isReviewScenario(requestedScenario) && requestedScenario !== "login" ? requestedScenario : "board";
  return {
    view,
    scenario,
    taskId: parseTaskRoute(pathname) ?? scenarioTaskId(scenario),
  };
}

export function bootReview(root = document.querySelector("#app")) {
  if (!root) throw new Error("Dish frontend root element is missing");
  document.title = DOCUMENT_TITLE;
  installFixtureReviewBoundary();
  document.documentElement.dataset.reviewMode = "true";
  const initial = resolveReviewInitialView();
  if (initial.view === "login") {
    renderLoginShell(root);
    root.prepend(createReviewToolbar("login"));
    return;
  }
  if (isAdminReviewScenario(initial.scenario)) {
    renderFixtureAdmin(root, initial.scenario);
    return;
  }
  renderFixturePrototype(root, initial.scenario, initial.taskId, { reviewMode: true });
}

void bootReview();
