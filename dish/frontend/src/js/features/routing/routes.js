export const BOARD_ROUTE = "/";
export const POSTGRES_TASK_ROUTE_PREFIX = "/dishes/";
const postgresRouteIdentity = /^(?!00000000-0000-0000-0000-000000000000)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function postgresSourceSuffix(search = globalThis.window?.location?.search ?? "") {
  const params = new URLSearchParams(search);
  const sources = params.getAll("source");
  return sources.length === 1 && sources[0] === "postgresql" ? "?source=postgresql" : "";
}

export function postgresTaskRoute(taskId, title) {
  if (!postgresRouteIdentity.test(taskId)) throw new Error("Invalid PostgreSQL task route identity");
  return `${POSTGRES_TASK_ROUTE_PREFIX}${taskId}/${titleSlug(title)}`;
}

export function parsePostgresTaskRoute(pathname) {
  if (!pathname.startsWith(POSTGRES_TASK_ROUTE_PREFIX)) return null;
  const parts = pathname.slice(POSTGRES_TASK_ROUTE_PREFIX.length).split("/");
  if (parts.length !== 2 || !postgresRouteIdentity.test(parts[0])) return null;
  return { taskId: parts[0], slug: parts[1] };
}

export function writePostgresRoute(path, mode = "replace", state = {}) {
  try {
    history[`${mode}State`](state, "", `${path}${postgresSourceSuffix()}`);
    return true;
  } catch {
    return false;
  }
}

export function titleSlug(title) {
  const slug = String(title ?? "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
  return slug || "task";
}
