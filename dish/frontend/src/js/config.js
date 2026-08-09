export const FRONTEND_CONTRACT_VERSION = "dish-frontend-v1";
export const DOCUMENT_TITLE = "Dish";
export const PROTOTYPE_LABEL = "Fixture prototype — not canonical data";

export const ACTIVE_REFRESH_MAX_SECONDS = 30;
export const ACTIVE_REFRESH_DEFAULT_SECONDS = 25;

export function activeRefreshIntervalMs(documentRoot = document) {
  const raw = documentRoot.querySelector('meta[name="dish-refresh-interval-seconds"]')?.content
    ?? String(ACTIVE_REFRESH_DEFAULT_SECONDS);
  const seconds = Number(raw);
  if (!Number.isInteger(seconds) || seconds < 1 || seconds > ACTIVE_REFRESH_MAX_SECONDS) {
    throw new Error("Dish active refresh interval is invalid");
  }
  return seconds * 1000;
}
