import { FrontendHttpClient } from "../api/http-transport.js";
import { mapArchiveResponse } from "../features/archive/archive-model.js";
import { renderArchive } from "../features/archive/archive.js";
import { renderInitialErrorState, renderLoadingState } from "../features/refresh/state-shells.js";
import { postgresSourceSuffix } from "../features/routing/routes.js";
import { createApplicationFrame } from "../shell/application-shell.js";

export async function renderLocalPostgresqlArchive(root, { environmentLabel = "LOCAL OBSERVATION", onAuthenticationLost = () => false } = {}) {
  const client = new FrontendHttpClient();
  const { shell, main } = createApplicationFrame({ environmentLabel, navigationSuffix: postgresSourceSuffix() });
  root.replaceChildren(shell); root.hidden = false; root.dataset.shellState = "local-postgresql-archive";
  let stopped = false;
  const refresh = async () => {
    if (stopped) return false;
    renderLoadingState(main);
    try {
      renderArchive(main, mapArchiveResponse(await client.archive()));
      root.dataset.shellState = "local-postgresql-archive"; return true;
    } catch (error) {
      if (onAuthenticationLost(error)) { stopped = true; return false; }
      renderInitialErrorState(main, () => void refresh(), { description: "Archive data could not be loaded." });
      root.dataset.shellState = "local-postgresql-archive-error"; return false;
    }
  };
  await refresh();
  return Object.freeze({ stop: () => { stopped = true; }, refresh });
}
