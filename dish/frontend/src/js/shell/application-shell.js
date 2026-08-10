import { applicationShellModel } from "./shell-model.js";

export function createApplicationFrame({ environmentLabel, navigationSuffix = "" } = {}) {
  const model = applicationShellModel();
  const shell = document.createElement("div");
  shell.className = "application-shell";
  shell.dataset.shellState = model.kind;

  const header = document.createElement("header");
  header.className = "app-header";
  const identity = document.createElement("div");
  identity.className = "app-header__identity";
  const mark = document.createElement("img");
  mark.className = "app-header__mark";
  mark.src = "/assets/dish-mark.svg";
  mark.alt = "";
  const heading = document.createElement("h1");
  heading.className = "app-header__title";
  heading.textContent = model.heading;
  identity.append(mark, heading);
  header.append(identity);

  const nav = document.createElement("nav");
  nav.className = "app-header__nav";
  nav.setAttribute("aria-label", "Primary");
  const boardLink = document.createElement("a");
  boardLink.href = `/${navigationSuffix}`;
  boardLink.textContent = "Cooking";
  const adminLink = document.createElement("a");
  adminLink.href = `/admin${navigationSuffix}`;
  adminLink.textContent = "Admin";
  nav.append(boardLink, adminLink);
  header.append(nav);
  if (environmentLabel) {
    const badge = document.createElement("span");
    badge.className = "environment-badge";
    badge.textContent = environmentLabel;
    header.append(badge);
  }

  const noticeHost = document.createElement("div");
  noticeHost.id = "notice-host";
  const utilityHost = document.createElement("div");
  utilityHost.id = "utility-host";
  const main = document.createElement("main");
  main.className = "shell-main";
  main.id = "board-shell";
  main.tabIndex = -1;
  shell.append(header, noticeHost, utilityHost, main);
  return { shell, main, noticeHost, utilityHost, model };
}

export function renderApplicationShell(root) {
  const { shell, main, model } = createApplicationFrame();
  const empty = document.createElement("section");
  empty.className = "empty-shell";
  empty.setAttribute("aria-labelledby", "empty-shell-heading");
  const content = document.createElement("div");
  const heading = document.createElement("h2");
  heading.id = "empty-shell-heading";
  heading.textContent = model.emptyHeading;
  const description = document.createElement("p");
  description.className = "muted";
  description.textContent = model.emptyDescription;
  content.append(heading, description);
  empty.append(content);
  main.append(empty);
  root.replaceChildren(shell);
  root.dataset.shellState = model.kind;
}
