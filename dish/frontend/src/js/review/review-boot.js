import { DOCUMENT_TITLE } from "../config.js";
import { renderFixtureAdmin, renderFixturePrototype } from "../prototype/prototype-app.js";
import { parseTaskRoute } from "../prototype/prototype-routes.js";
import { renderLoginShell } from "../shell/login-shell.js";
import { installFixtureReviewBoundary } from "./review-boundary.js";
import { isAdminReviewScenario, isReviewScenario, scenarioTaskId } from "./review-catalog.js";
import { createReviewToolbar } from "./review-toolbar.js";

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
    renderFixtureAdmin(root, initial.scenario, { reviewMode: true });
    return;
  }
  renderFixturePrototype(root, initial.scenario, initial.taskId, { reviewMode: true });
}

void bootReview();
