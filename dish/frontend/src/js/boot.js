import { DOCUMENT_TITLE } from "./config.js";
import { renderFixturePrototype } from "./prototype/prototype-app.js";
import { renderLoginShell } from "./shell/login-shell.js";

export function resolveInitialView(search = window.location.search) {
  const parameters = new URLSearchParams(search);
  return {
    view: parameters.get("view") === "login" ? "login" : "app",
    scenario: parameters.get("scenario") === "zero" ? "zero" : "board",
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
