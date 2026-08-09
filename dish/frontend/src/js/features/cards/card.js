import { cardAccessibleName, workflowStatusText } from "./card-model.js";

export function createTaskCard(card, { attentionLabels, onSelect }) {
  const button = document.createElement("button");
  button.className = "task-card";
  button.type = "button";
  button.dataset.taskId = card.id;
  button.setAttribute("aria-label", cardAccessibleName(card, attentionLabels));
  button.addEventListener("click", () => onSelect(card, button));

  const title = document.createElement("span");
  title.className = "task-card__title";
  title.textContent = card.title;

  const statusText = workflowStatusText(card.status);
  const status = statusText ? document.createElement("span") : null;
  if (status) {
    status.className = "task-card__status";
    status.textContent = statusText;
  }

  const indicators = document.createElement("span");
  indicators.className = "task-card__attention";
  for (const code of card.attention) {
    const label = attentionLabels[code];
    if (!label) continue;
    const indicator = document.createElement("span");
    indicator.className = `attention-chip attention-chip--${code}`;
    indicator.textContent = label;
    indicators.append(indicator);
  }

  button.append(title);
  if (status) button.append(status);
  button.append(indicators);
  return button;
}
