const taskPattern = /^(?!00000000-0000-0000-0000-000000000000)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export class ArchiveContractMismatch extends Error {
  constructor() {
    super("Archive response contract mismatch");
    this.name = "ArchiveContractMismatch";
    this.code = "contract_mismatch";
  }
}

function mismatch() { throw new ArchiveContractMismatch(); }
function object(value) { if (!value || typeof value !== "object" || Array.isArray(value)) mismatch(); return value; }
function exactKeys(value, required) {
  const keys = Object.keys(object(value));
  if (keys.length !== required.length || required.some((key) => !keys.includes(key))) mismatch();
}
function text(value, maximum) { if (typeof value !== "string" || value.length < 1 || value.length > maximum) mismatch(); return value; }
function date(value) { const result = text(value, 64); if (Number.isNaN(Date.parse(result))) mismatch(); return result; }
function log(value) { exactKeys(value, ["recorded_at", "text"]); return { recordedAt: date(value.recorded_at), text: text(value.text, 8000) }; }
function dish(value) {
  exactKeys(value, ["task_id", "title", "archived_at", "cook_logs"]);
  if (!taskPattern.test(value.task_id)) mismatch();
  if (!Array.isArray(value.cook_logs)) mismatch();
  return { id: value.task_id, title: text(value.title, 500), archivedAt: date(value.archived_at), cookLogs: value.cook_logs.map(log) };
}

export function mapArchiveResponse(value) {
  exactKeys(value, ["generated_at", "dishes", "truncated"]);
  if (!Array.isArray(value.dishes) || value.dishes.length > 5000 || typeof value.truncated !== "boolean") mismatch();
  const dishes = value.dishes.map(dish);
  if (new Set(dishes.map((item) => item.id)).size !== dishes.length) mismatch();
  return { generatedAt: date(value.generated_at), dishes, truncated: value.truncated };
}
