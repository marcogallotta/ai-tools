import { DOCUMENT_TITLE } from "./config.js";
import { renderFixturePrototype } from "./prototype/prototype-app.js";
import { installFixtureReviewBoundary } from "./review/review-boundary.js";
import { isReviewScenario, scenarioTaskId } from "./review/review-catalog.js";
import { createReviewToolbar } from "./review/review-toolbar.js";
import { parsePostgresTaskRoute, parseTaskRoute } from "./features/routing/routes.js";
import { renderLoginShell } from "./shell/login-shell.js";
import { renderLocalPostgresqlBoard } from "./local/local-board-app.js";
import { frontendDataSource } from "./local/source-selection.js";

export function resolveInitialView(search = window.location.search, pathname = window.location.pathname) {
  const parameters = new URLSearchParams(search);
  const requestedScenario = parameters.get("scenario") ?? "board";
  const view = parameters.get("view") === "login" ? "login" : "app";
  const scenario = isReviewScenario(requestedScenario) && requestedScenario !== "login" ? requestedScenario : "board";
  const dataSource = frontendDataSource(search);
  const postgresRoute = dataSource === "postgresql" ? parsePostgresTaskRoute(pathname) : null;
  return {
    view,
    scenario,
    reviewMode: parameters.get("review") === "1",
    taskId: parseTaskRoute(pathname) ?? scenarioTaskId(scenario),
    postgresTaskId: postgresRoute?.taskId ?? null,
    dataSource,
  };
}

export function boot(root = document.querySelector("#app")) {
  if (!root) throw new Error("Dish frontend root element is missing");
  document.title = DOCUMENT_TITLE;
  const initial = resolveInitialView();
  if (initial.reviewMode) installFixtureReviewBoundary();
  document.documentElement.dataset.reviewMode = String(initial.reviewMode);
  if (initial.view === "login") {
    renderLoginShell(root);
    if (initial.reviewMode) root.prepend(createReviewToolbar("login"));
    return;
  }
  if (initial.dataSource === "postgresql") {
    void renderLocalPostgresqlBoard(root, { initialTaskId: initial.postgresTaskId });
    return;
  }
  renderFixturePrototype(root, initial.scenario, initial.taskId, { reviewMode: initial.reviewMode });
}

boot();
