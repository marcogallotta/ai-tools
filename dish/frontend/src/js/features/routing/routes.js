export const BOARD_ROUTE = "/";
export const TASK_ROUTE_PREFIX = "/task/";
export const POSTGRES_TASK_ROUTE_PREFIX = "/dishes/";
const fixtureRouteIdentity = /^[a-z0-9][a-z0-9-]{0,79}$/;
const postgresRouteIdentity = /^(?!00000000-0000-0000-0000-000000000000)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function isPrototypeApplicationView(search) {
  return new URLSearchParams(search).get("view") === "app";
}

export function taskRoute(taskId) {
  if (!fixtureRouteIdentity.test(taskId)) throw new Error("Invalid fixture route identity");
  return `${TASK_ROUTE_PREFIX}${encodeURIComponent(taskId)}`;
}

export function parseTaskRoute(pathname) {
  if (pathname === BOARD_ROUTE) return null;
  if (!pathname.startsWith(TASK_ROUTE_PREFIX)) return null;
  const encoded = pathname.slice(TASK_ROUTE_PREFIX.length);
  if (!encoded || encoded.includes("/")) return null;
  let decoded;
  try { decoded = decodeURIComponent(encoded); } catch { return null; }
  return fixtureRouteIdentity.test(decoded) ? decoded : null;
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

export function writePrototypeRoute(path, mode = "replace", state = {}) {
  document.body.dataset.prototypeRoute = path;
  try {
    history[`${mode}State`](state, "", `${path}${window.location.search}`);
    return true;
  } catch {
    return false;
  }
}

export function writePostgresRoute(path, mode = "replace", state = {}) {
  try {
    history[`${mode}State`](state, "", `${path}${window.location.search}`);
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
