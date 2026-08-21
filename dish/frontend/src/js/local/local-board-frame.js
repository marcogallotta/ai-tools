import { mapAdminResponse } from "../features/admin/admin-model.js";
import { renderBoardAdminSummary } from "../features/admin/admin.js";
import { postgresSourceSuffix } from "../features/routing/routes.js";
import { createApplicationFrame } from "../shell/application-shell.js";

export function createLocalBoardFrame({ client, root, environmentLabel, onAuthenticationLost, onStop }) {
  const frame = createApplicationFrame({ environmentLabel, navigationSuffix: postgresSourceSuffix() });
  const searchHost = document.createElement("section");
  searchHost.className = "board-search";
  searchHost.setAttribute("aria-label", "Active dish search");
  const adminHost = document.createElement("div");
  adminHost.className = "board-admin-host";
  frame.utilityHost.append(searchHost, adminHost);
  const live = document.createElement("p");
  live.className = "sr-only";
  live.setAttribute("aria-live", "polite");
  frame.shell.append(live);
  root.replaceChildren(frame.shell); root.dataset.shellState = "local-postgresql-loading";

  const refreshAdminSummary = async () => {
    try {
      renderBoardAdminSummary(adminHost, mapAdminResponse(await client.admin()));
      const link = adminHost.querySelector(".board-admin-summary__link");
      if (link) link.href = `/admin${postgresSourceSuffix()}`;
    } catch (error) {
      if (onAuthenticationLost(error)) onStop();
      else adminHost.replaceChildren();
    }
  };

  return { ...frame, searchHost, live, refreshAdminSummary };
}
