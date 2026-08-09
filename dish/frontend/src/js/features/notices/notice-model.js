import { noticeRegistry } from "./notice-registry.js";

export function boardContributions(board) {
  return board.sections.flatMap((section) => section.cards.flatMap((card) => (
    card.attention.map((code) => ({ code, taskId: card.id }))
  )));
}

export function effectiveTaskContributions(board, detail = null) {
  const contributions = boardContributions(board);
  if (!detail) return contributions;
  const withoutSelectedCard = contributions.filter((item) => item.taskId !== detail.id);
  const detailContributions = detail.attention.map((code) => ({ code, taskId: detail.id }));
  if (detail.bodyPresentation?.state === "plain_text_fallback" || detail.contentMode === "plain_text_fallback") {
    detailContributions.push({ code: "render_rejected", taskId: detail.id });
  }
  return [...withoutSelectedCard, ...detailContributions];
}

export function groupNotices(contributions, lifecycle = []) {
  const groups = new Map();
  for (const contribution of [...contributions, ...lifecycle]) {
    const registered = noticeRegistry[contribution.code];
    if (!registered) throw new Error(`Unknown notice code: ${contribution.code}`);
    const existing = groups.get(contribution.code) ?? {
      code: contribution.code,
      label: registered.label,
      severity: registered.severity,
      taskIds: new Set(),
      message: contribution.message ?? null,
    };
    if (contribution.taskId) existing.taskIds.add(contribution.taskId);
    if (contribution.message) existing.message = contribution.message;
    groups.set(contribution.code, existing);
  }
  return [...groups.values()].map((group) => ({
    ...group,
    taskIds: [...group.taskIds],
    count: group.taskIds.size,
  }));
}

export function noticeHeading(notice) {
  if (notice.count > 1) return `${notice.label} — ${notice.count} tasks`;
  return notice.label;
}
