import { contentPresentation } from "./detail-model.js";

export function renderSafeContent(host, detail) {
  host.replaceChildren();
  const presentation = contentPresentation(detail);
  if (presentation.mode === "fallback") {
    const warning = document.createElement("p");
    warning.className = "detail-warning";
    warning.setAttribute("role", "status");
    warning.textContent = "Safe rendered content was unavailable. Showing inert plain text.";
    const pre = document.createElement("pre");
    pre.className = "detail-fallback";
    pre.textContent = presentation.text;
    host.append(warning, pre);
    return "fallback";
  }
  if (presentation.mode === "sanitized") {
    // Only api-detail-model validated backend-rendered body HTML reaches this branch.
    host.innerHTML = presentation.html;
    return "sanitized";
  }

  let list = null;
  for (const block of presentation.blocks) {
    if (block.kind === "list_item") {
      if (!list) {
        list = document.createElement("ul");
        host.append(list);
      }
      const item = document.createElement("li");
      item.textContent = block.text;
      list.append(item);
      continue;
    }
    list = null;
    const element = document.createElement(block.kind === "heading" ? "h3" : "p");
    element.textContent = block.text;
    host.append(element);
  }
  return "fixture";
}
