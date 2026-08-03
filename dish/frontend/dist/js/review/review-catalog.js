export const REVIEW_SCENARIOS = Object.freeze([
  { id: "board", label: "Normal board", description: "Populated board with pagination and an empty column." },
  { id: "attention", label: "All attention", description: "Every approved attention category on loaded cards." },
  { id: "detail", label: "Rendered detail", description: "Representative structured task detail." },
  { id: "fallback", label: "Rendering fallback", description: "Inert plain-text content fallback." },
  { id: "extreme", label: "Extreme content", description: "Long labels, titles, disclosures, and notices." },
  { id: "zero", label: "Zero sections", description: "Successful board with no active sections." },
  { id: "loading", label: "Loading", description: "Initial board loading presentation." },
  { id: "initial-error", label: "Initial failure", description: "No usable board and an explicit retry." },
  { id: "last-safe", label: "Refresh failure", description: "Last safe board retained beneath a warning." },
  { id: "login", label: "Login shell", description: "Unauthenticated Stage 0 shell; authentication is absent." },
]);

const scenarioIds = new Set(REVIEW_SCENARIOS.map((scenario) => scenario.id));
const scenarioTasks = Object.freeze({ detail: "task-biryani", fallback: "task-aubergine", extreme: "task-extreme" });

export function isReviewScenario(value) {
  return scenarioIds.has(value);
}

export function scenarioTaskId(id) {
  return scenarioTasks[id] ?? null;
}

export function scenarioHref(id, origin = window.location.origin) {
  const url = new URL(id === "login" ? "/" : scenarioTaskId(id) ? `/task/${scenarioTaskId(id)}` : "/", origin);
  url.searchParams.set("review", "1");
  if (id !== "login") url.searchParams.set("scenario", id);
  else url.searchParams.set("view", "login");
  return `${url.pathname}${url.search}`;
}
