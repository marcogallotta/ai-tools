import { DOCUMENT_TITLE } from "./config.js";
import { renderApplicationShell } from "./shell/application-shell.js";
import { renderLoginShell } from "./shell/login-shell.js";

export function resolveInitialView(search = window.location.search) {
  const requested = new URLSearchParams(search).get("view");
  return requested === "app" ? "app" : "login";
}

export function boot(root = document.querySelector("#app")) {
  if (!root) {
    throw new Error("Dish frontend root element is missing");
  }
  document.title = DOCUMENT_TITLE;
  const view = resolveInitialView();
  if (view === "app") {
    renderApplicationShell(root);
    return;
  }
  renderLoginShell(root);
}

boot();
