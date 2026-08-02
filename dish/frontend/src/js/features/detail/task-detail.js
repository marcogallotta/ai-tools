import { detailStatusText } from "./detail-model.js";
import { renderSafeContent } from "./safe-content.js";

let activePanel = null;

function definitionRow(label, value) {
  const wrapper = document.createElement("div");
  wrapper.className = "detail-fact";
  const term = document.createElement("dt");
  term.textContent = label;
  const description = document.createElement("dd");
  description.textContent = value;
  wrapper.append(term, description);
  return wrapper;
}

function renderDisclosures(host, disclosures) {
  if (!disclosures?.length) return;
  const heading = document.createElement("h3");
  heading.textContent = "Current facts needing attention";
  const list = document.createElement("div");
  list.className = "detail-disclosures";
  for (const disclosure of disclosures) {
    const item = document.createElement("section");
    const title = document.createElement("h4");
    title.textContent = disclosure.label;
    const detail = document.createElement("p");
    detail.textContent = disclosure.detail;
    item.append(title, detail);
    list.append(item);
  }
  host.append(heading, list);
}

function restorePanelFocus(origin, fallback) {
  if (origin?.isConnected) {
    origin.focus({ preventScroll: true });
    return;
  }
  const target = fallback?.isConnected ? fallback : document.querySelector('[aria-label="Dish task board"]');
  target?.focus({ preventScroll: true });
}

export function closeTaskDetail({ restoreFocus = true } = {}) {
  if (!activePanel) return;
  const { panel, origin, fallback, outsideListener, keyListener } = activePanel;
  document.removeEventListener("pointerdown", outsideListener, true);
  document.removeEventListener("keydown", keyListener);
  panel.remove();
  activePanel = null;
  if (restoreFocus) restorePanelFocus(origin, fallback);
}

export function openTaskDetail(detail, origin, { onRequestClose, focusFallback } = {}) {
  closeTaskDetail({ restoreFocus: false });
  const panel = document.createElement("aside");
  panel.className = "task-detail";
  panel.dataset.taskDetail = detail.id;
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "false");
  panel.setAttribute("aria-labelledby", "task-detail-title");

  const header = document.createElement("header");
  header.className = "task-detail__header";
  const identity = document.createElement("div");
  const eyebrow = document.createElement("p");
  eyebrow.className = "eyebrow";
  eyebrow.textContent = `${detail.projectLabel} / ${detail.sectionLabel}`;
  const heading = document.createElement("h2");
  heading.id = "task-detail-title";
  heading.textContent = detail.title;
  identity.append(eyebrow, heading);
  const close = document.createElement("button");
  close.className = "button button--secondary task-detail__close";
  close.type = "button";
  close.setAttribute("aria-label", "Close task detail");
  close.textContent = "Close";
  header.append(identity, close);

  const body = document.createElement("div");
  body.className = "task-detail__body";
  const facts = document.createElement("dl");
  facts.className = "detail-facts";
  facts.append(definitionRow("Status", detailStatusText(detail)));
  if (detail.destinationLabel) facts.append(definitionRow("Destination", detail.destinationLabel));
  const contentHeading = document.createElement("h3");
  contentHeading.textContent = "Current canonical content";
  const content = document.createElement("div");
  content.className = "detail-content";
  content.dataset.renderMode = renderSafeContent(content, detail);
  body.append(facts, contentHeading, content);
  renderDisclosures(body, detail.disclosures);
  const next = document.createElement("section");
  next.className = "detail-next-step";
  const nextHeading = document.createElement("h3");
  nextHeading.textContent = "What needs to happen next";
  const nextText = document.createElement("p");
  nextText.textContent = detail.nextStep;
  next.append(nextHeading, nextText);
  body.append(next);
  panel.append(header, body);
  document.body.append(panel);

  const requestClose = () => onRequestClose?.() ?? closeTaskDetail();
  close.addEventListener("click", requestClose);
  const outsideListener = (event) => {
    if (!panel.contains(event.target) && !event.target.closest?.(".task-card")) requestClose();
  };
  const keyListener = (event) => {
    if (event.key === "Escape") requestClose();
  };
  activePanel = { panel, origin, fallback: focusFallback, outsideListener, keyListener };
  document.addEventListener("pointerdown", outsideListener, true);
  document.addEventListener("keydown", keyListener);
  close.focus();
  return panel;
}
