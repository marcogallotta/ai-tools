import { loginShellModel } from "./shell-model.js";

export function renderLoginShell(root) {
  const model = loginShellModel();
  root.replaceChildren();
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
  form.dataset.prototypeLogin = "true";
  form.addEventListener("submit", (event) => event.preventDefault());

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
  input.disabled = true;
  field.append(label, input);

  const submit = document.createElement("button");
  submit.className = "button";
  submit.type = "submit";
  submit.disabled = true;
  submit.textContent = "Sign in";

  const note = document.createElement("p");
  note.className = "muted";
  note.textContent = `${model.prototypeLabel}. Authentication begins in Delivery Stage 2.`;

  form.append(field, submit);
  card.append(eyebrow, heading, description, form, note);
  layout.append(card);
  root.append(layout);
}
