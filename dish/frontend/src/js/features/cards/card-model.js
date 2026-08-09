export function workflowStatusText(status) {
  if (status.state === "no_active_operation") {
    return "";
  }
  return status.phase ? `${status.operation} · ${status.phase}` : status.operation;
}

export function cardAccessibleName(card, attentionLabels) {
  const attention = card.attention.map((code) => attentionLabels[code]).filter(Boolean);
  return [card.title, workflowStatusText(card.status), ...attention].filter(Boolean).join(". ");
}
