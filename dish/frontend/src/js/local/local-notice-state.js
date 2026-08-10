import { effectiveTaskContributions, groupNotices } from "../features/notices/notice-model.js";
import { noticeRegistry } from "../features/notices/notice-registry.js";
import { renderNotices } from "../features/notices/notices.js";

export const localAttentionLabels = Object.freeze(Object.fromEntries(
  Object.entries(noticeRegistry).map(([code, presentation]) => [code, presentation.label]),
));

export function createLocalNoticeState({
  noticeHost,
  boardValue,
  detailValue,
  onSelectTask,
  onRetry,
  onReload,
}) {
  const requestNotices = new Map();
  let lastSignature = null;

  const render = () => {
    const detail = detailValue();
    const serverDetail = detail?.notices?.map((notice) => ({ ...notice })) ?? [];
    const notices = groupNotices(
      effectiveTaskContributions(boardValue() ?? { sections: [] }, detail),
      [...serverDetail, ...requestNotices.values()],
    );
    const signature = JSON.stringify(notices.map((notice) => [notice.code, notice.message, notice.action, notice.tasks]));
    if (signature === lastSignature) return;
    lastSignature = signature;
    renderNotices(noticeHost, notices, { onSelectTask, onRetry, onReload });
  };

  return Object.freeze({
    render,
    set(scope, notice) { requestNotices.set(scope, notice); render(); },
    clear(scope) { if (requestNotices.delete(scope)) render(); },
  });
}
