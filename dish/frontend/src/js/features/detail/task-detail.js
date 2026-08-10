import { detailStatusText } from "./detail-model.js";
import { renderSafeContent } from "./safe-content.js";
import { noticeRegistry } from "../notices/notice-registry.js";
import { postgresSourceSuffix } from "../routing/routes.js";

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
  delete document.body.dataset.detailOpen;
  activePanel = null;
  if (restoreFocus) restorePanelFocus(origin, fallback);
}

export function openTaskDetail(detail, origin, { onRequestClose, focusFallback, refresh = false } = {}) {
  const prior = refresh && activePanel?.panel?.dataset.taskDetail === detail.id ? {
    scrollTop: activePanel.panel.querySelector(".task-detail__body")?.scrollTop ?? 0,
    technicalOpen: Boolean(activePanel.panel.querySelector(".detail-technical")?.open),
    processOpen: Boolean(activePanel.panel.querySelector(".canonical-process-record")?.open),
    focusInside: activePanel.panel.contains(document.activeElement),
  } : null;
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
  const statusText = detailStatusText(detail);
  if (statusText) facts.append(definitionRow("Status", statusText));
  if (detail.destinationLabel) facts.append(definitionRow("Destination", detail.destinationLabel));
  if (detail.attention?.length) {
    const adminStrip = document.createElement("section");
    adminStrip.className = "detail-admin-strip";
    const adminText = document.createElement("p");
    const labels = detail.attention.map((code) => noticeRegistry[code]?.label).filter(Boolean);
    adminText.textContent = `Admin: ${labels.join(" · ")}`;
    const adminLink = document.createElement("a");
    adminLink.href = `/admin${postgresSourceSuffix()}#dish=${encodeURIComponent(detail.id)}`;
    adminLink.textContent = "Open in Admin";
    adminStrip.append(adminText, adminLink);
    body.append(adminStrip);
  }
  const advisory = detail.advisory
    ? (detail.advisory.code !== "workflow.none" ? detail.advisory : null)
    : (detail.nextStep ? { message: detail.nextStep } : null);
  if (advisory) {
    const next = document.createElement("section");
    next.className = "detail-next-step";
    const nextHeading = document.createElement("h3");
    nextHeading.textContent = "What needs to happen next";
    const nextText = document.createElement("p");
    nextText.textContent = advisory.message;
    next.append(nextHeading, nextText);
    body.append(next);
  }
  const contentHeading = document.createElement("h3");
  contentHeading.textContent = "Current canonical content";
  const content = document.createElement("div");
  content.className = "detail-content";
  content.dataset.renderMode = renderSafeContent(content, detail);
  if (prior?.processOpen) {
    const processRecord = content.querySelector(".canonical-process-record");
    if (processRecord) processRecord.open = true;
  }
  if (facts.childElementCount) body.append(facts);
  body.append(contentHeading, content);
  if (detail.disclosures?.length || detail.projection) {
    const technical = document.createElement("details");
    technical.className = "detail-technical";
    const summary = document.createElement("summary");
    summary.textContent = "Technical details";
    technical.append(summary);
    if (prior?.technicalOpen) technical.open = true;
    renderDisclosures(technical, detail.disclosures);
    if (detail.projection) {
      const projection = document.createElement("section");
      projection.className = "detail-projection";
      const projectionHeading = document.createElement("h3");
      projectionHeading.textContent = `Projection — ${detail.projection.state}`;
      const projectionText = document.createElement("p");
      projectionText.textContent = detail.projection.message;
      projection.append(projectionHeading, projectionText);
      technical.append(projection);
    }
    body.append(technical);
  }
  panel.append(header, body);
  document.body.append(panel);
  document.body.dataset.detailOpen = "true";

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
  if (prior) {
    body.scrollTop = prior.scrollTop;
    if (prior.focusInside) close.focus({ preventScroll: true });
  } else {
    close.focus();
  }
  return panel;
}
