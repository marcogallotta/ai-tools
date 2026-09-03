import { DOCUMENT_TITLE } from "./config.js";
import { parsePostgresTaskRoute } from "./features/routing/routes.js";
import { renderLocalPostgresqlAdmin } from "./local/local-admin-app.js";
import { renderLocalPostgresqlArchive } from "./local/local-archive-app.js";
import { renderLocalPostgresqlBoard } from "./local/local-board-app.js";
import { frontendDataSource } from "./local/source-selection.js";
import { bootPrivateFrontend } from "./private/private-app.js";
import { createApplicationFrame } from "./shell/application-shell.js";

export function runtimeMode(documentRoot = document) {
  return documentRoot.querySelector('meta[name="dish-runtime-mode"]')?.content ?? "local-observation";
}

function renderLocalSourceRequired(root) {
  const { shell, main } = createApplicationFrame({ environmentLabel: "LOCAL OBSERVATION" });
  const section = document.createElement("section");
  section.className = "empty-shell";
  const content = document.createElement("div");
  const heading = document.createElement("h2");
  heading.textContent = "PostgreSQL observation source not selected";
  const description = document.createElement("p");
  description.className = "muted";
  description.textContent = "Open this local build with ?source=postgresql to inspect the read-only PostgreSQL observation surface.";
  content.append(heading, description);
  section.append(content);
  main.append(section);
  root.replaceChildren(shell);
  root.hidden = false;
  root.dataset.shellState = "local-source-required";
}

export async function boot(root = document.querySelector("#app")) {
  if (!root) throw new Error("Dish frontend root element is missing");
  document.title = DOCUMENT_TITLE;
  const mode = runtimeMode();

  if (["private-disabled", "private-postgresql", "private-postgresql-authority"].includes(mode)) {
    await bootPrivateFrontend(root, { mode });
    return;
  }
  if (mode !== "local-observation") throw new Error("Dish frontend runtime mode is invalid");
  if (frontendDataSource(window.location.search) !== "postgresql") {
    renderLocalSourceRequired(root);
    return;
  }
  if (window.location.pathname === "/admin") {
    await renderLocalPostgresqlAdmin(root);
    return;
  }
  if (window.location.pathname === "/cooked") {
    await renderLocalPostgresqlArchive(root);
    return;
  }
  const route = parsePostgresTaskRoute(window.location.pathname);
  await renderLocalPostgresqlBoard(root, { initialTaskId: route?.taskId ?? null });
}

void boot();
