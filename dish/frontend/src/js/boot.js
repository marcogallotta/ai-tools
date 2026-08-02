import { DOCUMENT_TITLE } from "./config.js";
import { renderFixturePrototype } from "./prototype/prototype-app.js";
import { renderLoginShell } from "./shell/login-shell.js";

const scenarios = new Set(["board", "zero", "loading", "initial-error", "last-safe"]);

export function resolveInitialView(search = window.location.search) {
  const parameters = new URLSearchParams(search);
  const requestedScenario = parameters.get("scenario") ?? "board";
  return {
    view: parameters.get("view") === "login" ? "login" : "app",
    scenario: scenarios.has(requestedScenario) ? requestedScenario : "board",
  };
}

export function boot(root = document.querySelector("#app")) {
  if (!root) throw new Error("Dish frontend root element is missing");
  document.title = DOCUMENT_TITLE;
  const initial = resolveInitialView();
  if (initial.view === "login") {
    renderLoginShell(root);
    return;
  }
  renderFixturePrototype(root, initial.scenario);
}

boot();
