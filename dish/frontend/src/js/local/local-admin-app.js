import { FrontendHttpClient } from "../api/http-transport.js";
import { mapAdminResponse } from "../features/admin/admin-model.js";
import { renderAdmin } from "../features/admin/admin.js";
import { renderInitialErrorState, renderLoadingState } from "../features/refresh/state-shells.js";
import { postgresSourceSuffix } from "../features/routing/routes.js";
import { createApplicationFrame } from "../shell/application-shell.js";

export async function renderLocalPostgresqlAdmin(root, { environmentLabel = "LOCAL OBSERVATION", onAuthenticationLost = () => false } = {}) {
  const client = new FrontendHttpClient();
  const { shell, main } = createApplicationFrame({ environmentLabel, navigationSuffix: postgresSourceSuffix() });
  root.replaceChildren(shell); root.hidden = false; root.dataset.shellState = "local-postgresql-admin";
  let stopped = false;
  const refresh = async () => {
    if (stopped) return false;
    renderLoadingState(main);
    try {
      renderAdmin(main, mapAdminResponse(await client.admin()));
      root.dataset.shellState = "local-postgresql-admin"; return true;
    } catch (error) {
      if (onAuthenticationLost(error)) { stopped = true; return false; }
      renderInitialErrorState(main, () => void refresh(), { description: "Admin data could not be loaded." });
      root.dataset.shellState = "local-postgresql-admin-error"; return false;
    }
  };
  await refresh();
  return Object.freeze({ stop: () => { stopped = true; }, refresh });
}
