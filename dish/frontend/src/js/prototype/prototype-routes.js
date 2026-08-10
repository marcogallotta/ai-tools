export const BOARD_ROUTE = "/";
export const TASK_ROUTE_PREFIX = "/task/";
const fixtureRouteIdentity = /^[a-z0-9][a-z0-9-]{0,79}$/;

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

export function writePrototypeRoute(path, mode = "replace", state = {}) {
  document.body.dataset.prototypeRoute = path;
  try {
    history[`${mode}State`](state, "", `${path}${window.location.search}`);
    return true;
  } catch {
    return false;
  }
}
