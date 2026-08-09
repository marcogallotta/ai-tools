import * as boardContract from "../board/api-board-model.js";
import { noticeRegistry } from "../notices/notice-registry.js";

const detailTaskRoutePattern = /^r1t-[A-Za-z0-9_-]{27}$/;
const attentionOrder = Object.freeze([
  "isolated",
  "lease_attention",
  "verification_attention",
  "hold_active",
  "recovery_required",
  "abandonment_active",
  "succession_active",
  "projection_abnormal",
]);
const disclosureOrder = Object.freeze(["lease", "verification", "hold", "recovery", "abandonment", "succession"]);
const projectionStates = new Set(["delayed", "failed", "drifted", "unknown", "unavailable"]);
const operationLabels = new Set(boardContract.WORKFLOW_OPERATION_LABELS);
const phaseLabels = new Set(boardContract.WORKFLOW_PHASE_LABELS);

export class DetailContractMismatch extends Error {
  constructor(message = "Task detail response contract mismatch") {
    super(message);
    this.name = "DetailContractMismatch";
    this.code = "contract_mismatch";
  }
}

function mismatch() { throw new DetailContractMismatch(); }
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
  if (typeof value !== "string" || Array.from(value).length < 1 || Array.from(value).length > maximum) mismatch();
  return value;
}
function route(value) {
  const result = boundedString(value, 200);
  if (!detailTaskRoutePattern.test(result)) mismatch();
  return result;
}
function workflowStatus(value) {
  const status = object(value);
  if (status.state === "no_active_operation") {
    exactKeys(status, ["state"]);
    return { state: status.state };
  }
  exactKeys(status, ["state", "operation", "phase"]);
  if (status.state !== "active_operation") mismatch();
  const operation = boundedString(status.operation, boardContract.WORKFLOW_LABEL_MAX_LENGTH);
  const phase = boundedString(status.phase, boardContract.WORKFLOW_LABEL_MAX_LENGTH);
  if (!operationLabels.has(operation) || !phaseLabels.has(phase)) mismatch();
  return { state: status.state, operation, phase };
}
function attention(value) {
  if (!Array.isArray(value) || value.length > attentionOrder.length || new Set(value).size !== value.length) mismatch();
  if (value.some((code) => !attentionOrder.includes(code))) mismatch();
  const active = new Set(value);
  const normalized = attentionOrder.filter((code) => active.has(code));
  if (normalized.some((code, index) => code !== value[index])) mismatch();
  return normalized;
}
function bodyPresentation(value) {
  const body = object(value);
  if (body.state === "sanitized_html") {
    exactKeys(body, ["state", "html"]);
    if (typeof body.html !== "string" || Array.from(body.html).length > 300000) mismatch();
    return { state: body.state, html: body.html };
  }
  if (body.state === "plain_text_fallback") {
    exactKeys(body, ["state", "text"]);
    if (typeof body.text !== "string" || Array.from(body.text).length > 100000) mismatch();
    return { state: body.state, text: body.text };
  }
  mismatch();
}
function disclosures(value) {
  if (!Array.isArray(value) || value.length > 20) mismatch();
  let prior = -1;
  return value.map((item) => {
    exactKeys(item, ["code", "label", "detail"]);
    const index = disclosureOrder.indexOf(item.code);
    if (index < prior || index < 0) mismatch();
    prior = index;
    return {
      code: item.code,
      label: boundedString(item.label, 120),
      detail: boundedString(item.detail, 1000),
    };
  });
}
function advisory(value) {
  exactKeys(value, ["code", "message", "perspective", "invokable_by_frontend"]);
  if (value.perspective !== "workflow" || value.invokable_by_frontend !== false) mismatch();
  return {
    code: boundedString(value.code, 80),
    message: boundedString(value.message, 1000),
    perspective: "workflow",
    invokableByFrontend: false,
  };
}
function projection(value) {
  if (value === null) return null;
  exactKeys(value, ["state", "message"], ["observation_time"]);
  if (!projectionStates.has(value.state)) mismatch();
  return {
    state: value.state,
    message: boundedString(value.message, 1000),
    observationTime: value.observation_time == null ? null : boundedString(value.observation_time, 64),
  };
}
function notices(value, taskId, attentionCodes, body) {
  if (!Array.isArray(value) || value.length > 9) mismatch();
  const expected = new Set(attentionCodes);
  if (body.state === "plain_text_fallback") expected.add("render_rejected");
  if (value.length !== expected.size) mismatch();
  const seen = new Set();
  return value.map((item) => {
    exactKeys(item, ["code", "severity", "message", "target"]);
    if (!expected.has(item.code) || seen.has(item.code) || !noticeRegistry[item.code]) mismatch();
    if (noticeRegistry[item.code].severity !== item.severity) mismatch();
    exactKeys(item.target, ["type", "route_identity"]);
    if (item.target.type !== "task" || route(item.target.route_identity) !== taskId) mismatch();
    seen.add(item.code);
    return { code: item.code, severity: item.severity, message: boundedString(item.message, 1000), taskId };
  });
}

export function mapTaskDetailResponse(value) {
  exactKeys(value, [
    "task_id", "title", "project_label", "section_label", "workflow_status", "attention_codes",
    "body_presentation", "disclosures", "advisory", "projection", "notices",
  ], ["destination_label"]);
  const id = route(value.task_id);
  const body = bodyPresentation(value.body_presentation);
  const attentionCodes = attention(value.attention_codes);
  const mappedDisclosures = disclosures(value.disclosures);
  const available = new Set(mappedDisclosures.map((item) => item.code));
  const required = {
    lease_attention: "lease", verification_attention: "verification", hold_active: "hold",
    recovery_required: "recovery", abandonment_active: "abandonment", succession_active: "succession",
  };
  for (const code of attentionCodes) if (required[code] && !available.has(required[code])) mismatch();
  const mappedProjection = projection(value.projection);
  if (attentionCodes.includes("projection_abnormal") !== Boolean(mappedProjection)) mismatch();
  const mappedNotices = notices(value.notices, id, attentionCodes, body);
  return {
    id,
    title: boundedString(value.title, 500),
    projectLabel: boundedString(value.project_label, 160),
    sectionLabel: boundedString(value.section_label, 160),
    destinationLabel: value.destination_label == null ? null : boundedString(value.destination_label, 160),
    status: workflowStatus(value.workflow_status),
    attention: attentionCodes,
    bodyPresentation: body,
    disclosures: mappedDisclosures,
    advisory: advisory(value.advisory),
    projection: mappedProjection,
    notices: mappedNotices,
  };
}
