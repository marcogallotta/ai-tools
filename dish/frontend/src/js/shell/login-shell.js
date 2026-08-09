import { loginShellModel } from "./shell-model.js";

export function renderLoginShell(root, {
  onSubmit = null,
  pending = false,
  message = "",
  retryAfterSeconds = null,
} = {}) {
  const model = loginShellModel();
  root.replaceChildren();
  root.hidden = false;
  root.dataset.shellState = model.kind;

  const layout = document.createElement("div");
  layout.className = "login-layout";
  const card = document.createElement("section");
  card.className = "panel-card";
  card.setAttribute("aria-labelledby", "login-heading");

  const eyebrow = document.createElement("p");
  eyebrow.className = "eyebrow";
  eyebrow.textContent = "Private access";
  const heading = document.createElement("h1");
  heading.id = "login-heading";
  heading.textContent = model.heading;
  const description = document.createElement("p");
  description.className = "muted";
  description.textContent = model.description;

  const form = document.createElement("form");
  const field = document.createElement("div");
  field.className = "field";
  const label = document.createElement("label");
  label.htmlFor = "shared-password";
  label.textContent = "Shared password";
  const input = document.createElement("input");
  input.id = "shared-password";
  input.name = "password";
  input.type = "password";
  input.autocomplete = "current-password";
  input.disabled = pending || typeof onSubmit !== "function";
  input.maxLength = 1024;
  field.append(label, input);

  const submit = document.createElement("button");
  submit.className = "button";
  submit.type = "submit";
  submit.disabled = input.disabled;
  submit.textContent = pending ? "Signing in…" : "Sign in";
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!submit.disabled) void onSubmit(input.value);
  });

  const status = document.createElement("p");
  status.className = "muted";
  status.setAttribute("role", "status");
  status.textContent = retryAfterSeconds === null ? message : `${message} Try again in ${retryAfterSeconds} seconds.`;
  form.append(field, submit, status);
  card.append(eyebrow, heading, description, form);
  layout.append(card);
  root.append(layout);
  if (!input.disabled) input.focus();
}

export function renderLogoutPendingShell(root, { onRetry }) {
  root.replaceChildren();
  root.hidden = false;
  root.dataset.shellState = "logout-unresolved";
  const layout = document.createElement("div");
  layout.className = "login-layout";
  const card = document.createElement("section");
  card.className = "panel-card";
  const heading = document.createElement("h1");
  heading.textContent = "Sign out not confirmed";
  const description = document.createElement("p");
  description.className = "muted";
  description.textContent = "Protected content remains concealed until sign out is resolved.";
  const retry = document.createElement("button");
  retry.type = "button";
  retry.className = "button";
  retry.textContent = "Retry sign out";
  retry.addEventListener("click", () => void onRetry());
  card.append(heading, description, retry);
  layout.append(card);
  root.append(layout);
}
