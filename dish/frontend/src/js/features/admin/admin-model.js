import { WORKFLOW_OPERATION_LABELS, WORKFLOW_PHASE_LABELS } from "../board/api-board-model.js";
import { TASK_ATTENTION_NOTICE_CODES } from "../notices/notice-registry.js";

const taskPattern = /^(?!00000000-0000-0000-0000-000000000000)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const operationLabels = new Set(WORKFLOW_OPERATION_LABELS);
const phaseLabels = new Set(WORKFLOW_PHASE_LABELS);
const attentionCodes = new Set(TASK_ATTENTION_NOTICE_CODES);
const buckets = new Set(["needs_you", "system_activity"]);

export class AdminContractMismatch extends Error {
  constructor() {
    super("Admin response contract mismatch");
    this.name = "AdminContractMismatch";
    this.code = "contract_mismatch";
  }
}

function mismatch() { throw new AdminContractMismatch(); }
function object(value) { if (!value || typeof value !== "object" || Array.isArray(value)) mismatch(); return value; }
function exactKeys(value, required) {
  const keys = Object.keys(object(value));
  if (keys.length !== required.length || required.some((key) => !keys.includes(key))) mismatch();
}
function string(value, max) { if (typeof value !== "string" || value.length < 1 || value.length > max) mismatch(); return value; }
function uuid(value, task = false) { const text = string(value, 64); if (!(task ? taskPattern : uuidPattern).test(text)) mismatch(); return text; }
function date(value, nullable = false) {
  if (nullable && value === null) return null;
  const text = string(value, 64); if (Number.isNaN(Date.parse(text))) mismatch(); return text;
}
function count(value) { if (!Number.isInteger(value) || value < 0) mismatch(); return value; }
function workflow(value) {
  const state = object(value);
  if (state.state === "no_active_operation") { exactKeys(state, ["state"]); return { state: state.state }; }
  exactKeys(state, ["state", "operation", "phase"]);
  if (state.state !== "active_operation" || !operationLabels.has(state.operation) || !phaseLabels.has(state.phase)) mismatch();
  return { state: state.state, operation: state.operation, phase: state.phase };
}
function diagnostics(value) {
  exactKeys(value, ["attention_codes"]);
  if (!Array.isArray(value.attention_codes) || value.attention_codes.length > 8 || new Set(value.attention_codes).size !== value.attention_codes.length) mismatch();
  if (value.attention_codes.some((code) => !attentionCodes.has(code))) mismatch();
  return { attentionCodes: [...value.attention_codes] };
}
function attention(value) {
  exactKeys(value, ["code", "label", "message"]);
  if (!attentionCodes.has(value.code)) mismatch();
  return { code: value.code, label: string(value.label, 120), message: string(value.message, 300) };
}
function dish(value) {
  exactKeys(value, ["task_id", "title", "section_label", "workflow_status", "bucket", "attention", "last_activity_at", "diagnostics"]);
  if (!buckets.has(value.bucket) || !Array.isArray(value.attention) || value.attention.length < 1 || value.attention.length > 8) mismatch();
  const mappedAttention = value.attention.map(attention);
  if (new Set(mappedAttention.map((item) => item.code)).size !== mappedAttention.length) mismatch();
  const mappedDiagnostics = diagnostics(value.diagnostics);
  if (mappedDiagnostics.attentionCodes.length !== mappedAttention.length || mappedAttention.some((item) => !mappedDiagnostics.attentionCodes.includes(item.code))) mismatch();
  return {
    id: uuid(value.task_id, true), title: string(value.title, 500), sectionLabel: string(value.section_label, 160),
    status: workflow(value.workflow_status), bucket: value.bucket, attention: mappedAttention,
    lastActivityAt: date(value.last_activity_at, true), diagnostics: mappedDiagnostics,
  };
}
function journalDiagnostics(value) {
  exactKeys(value, ["event_type", "actor", "request_id", "command_execution_id", "operation_id"]);
  return {
    eventType: string(value.event_type, 96), actor: string(value.actor, 256), requestId: uuid(value.request_id),
    commandExecutionId: value.command_execution_id === null ? null : uuid(value.command_execution_id),
    operationId: value.operation_id === null ? null : uuid(value.operation_id),
  };
}
function event(value) {
  exactKeys(value, ["event_id", "task_id", "title", "occurred_at", "summary", "diagnostics"]);
  return {
    id: uuid(value.event_id), taskId: uuid(value.task_id, true), title: string(value.title, 500),
    occurredAt: date(value.occurred_at), summary: string(value.summary, 220), diagnostics: journalDiagnostics(value.diagnostics),
  };
}

export function mapAdminResponse(value) {
  exactKeys(value, ["generated_at", "summary", "dishes", "journal"]);
  exactKeys(value.summary, ["needs_you", "human_review", "recovery", "system_activity", "affected_dishes"]);
  if (!Array.isArray(value.dishes) || value.dishes.length > 5000 || !Array.isArray(value.journal) || value.journal.length > 120) mismatch();
  const dishes = value.dishes.map(dish);
  if (new Set(dishes.map((item) => item.id)).size !== dishes.length) mismatch();
  const summary = {
    needsYou: count(value.summary.needs_you), humanReview: count(value.summary.human_review), recovery: count(value.summary.recovery),
    systemActivity: count(value.summary.system_activity), affectedDishes: count(value.summary.affected_dishes),
  };
  if (summary.affectedDishes !== dishes.length || summary.needsYou + summary.systemActivity !== dishes.length) mismatch();
  if (summary.needsYou !== dishes.filter((item) => item.bucket === "needs_you").length) mismatch();
  return { generatedAt: date(value.generated_at), summary, dishes, journal: value.journal.map(event) };
}

export function workflowText(status) {
  return status.state === "active_operation" ? `${status.operation} · ${status.phase}` : "No active operation";
}
