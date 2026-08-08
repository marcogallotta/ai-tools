import { noticeRegistry } from "../notices/notice-registry.js";

const routePatterns = Object.freeze({
  task: /^r1t-[A-Za-z0-9_-]{27}$/,
  section: /^r1s-[A-Za-z0-9_-]{27}$/,
});
const attentionOrder = Object.freeze(Object.keys(noticeRegistry).filter((code) => (
  !["rendering_fallback", "initial_load_failed", "service_unavailable"].includes(code)
)));
export const WORKFLOW_OPERATION_LABELS = Object.freeze([
  "Planning",
  "Initial",
  "Change",
  "Verification",
  "Migration",
]);
export const WORKFLOW_PHASE_LABELS = Object.freeze([
  "Prepare required",
  "Await verification",
  "Evidence hold",
  "Human review",
  "Await submission",
  "Destination repair required",
  "Recovery rehearsal",
]);
export const WORKFLOW_LABEL_MAX_LENGTH = 80;
const workflowOperationLabels = new Set(WORKFLOW_OPERATION_LABELS);
const workflowPhaseLabels = new Set(WORKFLOW_PHASE_LABELS);

export class BoardContractMismatch extends Error {
  constructor(message = "Board response contract mismatch") {
    super(message);
    this.name = "BoardContractMismatch";
    this.code = "contract_mismatch";
  }
}

function mismatch() {
  throw new BoardContractMismatch();
}

function object(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) mismatch();
  return value;
}

function exactKeys(value, required, optional = []) {
  const keys = Object.keys(object(value));
  const allowed = new Set([...required, ...optional]);
  if (required.some((key) => !keys.includes(key)) || keys.some((key) => !allowed.has(key))) mismatch();
}

function boundedString(value, maximum) {
  if (typeof value !== "string") mismatch();
  const length = Array.from(value).length;
  if (length < 1 || length > maximum) mismatch();
  return value;
}

function route(value, kind) {
  const text = boundedString(value, 200);
  if (!routePatterns[kind].test(text)) mismatch();
  return text;
}

function workflowStatus(value) {
  const status = object(value);
  if (status.state === "no_active_operation") {
    exactKeys(status, ["state"]);
    return { state: status.state };
  }
  if (status.state !== "active_operation") mismatch();
  exactKeys(status, ["state", "operation", "phase"]);
  const operation = boundedString(status.operation, WORKFLOW_LABEL_MAX_LENGTH);
  const phase = boundedString(status.phase, WORKFLOW_LABEL_MAX_LENGTH);
  if (!workflowOperationLabels.has(operation) || !workflowPhaseLabels.has(phase)) mismatch();
  return { state: status.state, operation, phase };
}

function attention(value) {
  if (!Array.isArray(value) || value.length > attentionOrder.length) mismatch();
  if (new Set(value).size !== value.length || value.some((code) => !attentionOrder.includes(code))) mismatch();
  const active = new Set(value);
  return attentionOrder.filter((code) => active.has(code));
}

function card(value, containingSectionId) {
  exactKeys(value, ["task_id", "title", "section_id", "workflow_status", "attention_codes"]);
  const sectionId = route(value.section_id, "section");
  if (sectionId !== containingSectionId) mismatch();
  return {
    id: route(value.task_id, "task"),
    title: boundedString(value.title, 500),
    status: workflowStatus(value.workflow_status),
    attention: attention(value.attention_codes),
  };
}

function validateNotices(rawNotices, cards) {
  if (!Array.isArray(rawNotices)) mismatch();
  const expected = new Map();
  for (const current of cards) {
    for (const code of current.attention) expected.set(`${code}\0${current.id}`, noticeRegistry[code].severity);
  }
  if (rawNotices.length !== expected.size) mismatch();
  const seen = new Set();
  for (const notice of rawNotices) {
    exactKeys(notice, ["code", "task_id", "severity"]);
    const taskId = route(notice.task_id, "task");
    const key = `${notice.code}\0${taskId}`;
    if (seen.has(key) || expected.get(key) !== notice.severity) mismatch();
    seen.add(key);
  }
}

function cursor(value) {
  if (value === null) return null;
  return boundedString(value, 4096);
}

export function mapBoardResponse(value) {
  exactKeys(value, ["snapshot_id", "page_size", "sections", "notices"]);
  boundedString(value.snapshot_id, 200);
  if (!Number.isInteger(value.page_size) || value.page_size < 1 || value.page_size > 100) mismatch();
  if (!Array.isArray(value.sections) || value.sections.length > 100) mismatch();
  const sectionIds = new Set();
  const taskIds = new Set();
  const sections = value.sections.map((raw) => {
    exactKeys(raw, ["section_id", "section_label", "continuity_id", "cards", "next_cursor"], ["project_label"]);
    const id = route(raw.section_id, "section");
    if (sectionIds.has(id)) mismatch();
    sectionIds.add(id);
    if (!Array.isArray(raw.cards) || raw.cards.length > value.page_size) mismatch();
    const cards = raw.cards.map((item) => card(item, id));
    for (const current of cards) {
      if (taskIds.has(current.id)) mismatch();
      taskIds.add(current.id);
    }
    const nextCursor = cursor(raw.next_cursor);
    if (cards.length === 0 && nextCursor !== null) mismatch();
    return {
      id,
      label: boundedString(raw.section_label, 160),
      projectLabel: raw.project_label == null ? null : boundedString(raw.project_label, 160),
      continuityId: boundedString(raw.continuity_id, 200),
      cards,
      nextCursor,
      hasMore: nextCursor !== null,
    };
  });
  validateNotices(value.notices, sections.flatMap((section) => section.cards));
  return { snapshotId: value.snapshot_id, pageSize: value.page_size, sections };
}

export function mapSectionPageResponse(value, section) {
  exactKeys(value, ["section_id", "continuity_id", "cards", "next_cursor", "notices"]);
  if (route(value.section_id, "section") !== section.id) mismatch();
  if (boundedString(value.continuity_id, 200) !== section.continuityId) mismatch();
  if (!Array.isArray(value.cards) || value.cards.length > 100) mismatch();
  const cards = value.cards.map((item) => card(item, section.id));
  if (cards.length === 0 && value.next_cursor !== null) mismatch();
  if (new Set(cards.map((item) => item.id)).size !== cards.length) mismatch();
  validateNotices(value.notices, cards);
  return { cards, nextCursor: cursor(value.next_cursor) };
}

export function appendSectionPage(board, sectionId, page) {
  const section = board.sections.find((item) => item.id === sectionId);
  if (!section) mismatch();
  const existing = new Set(board.sections.flatMap((item) => item.cards.map((cardItem) => cardItem.id)));
  if (page.cards.some((cardItem) => existing.has(cardItem.id))) mismatch();
  return {
    ...board,
    sections: board.sections.map((item) => item.id === sectionId ? {
      ...item,
      cards: [...item.cards, ...page.cards],
      nextCursor: page.nextCursor,
      hasMore: page.nextCursor !== null,
    } : item),
  };
}
