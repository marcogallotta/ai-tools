import { FrontendApiError, FrontendHttpClient } from "../api/http-transport.js";
import {
  PrivateSessionLifecycle,
  isSessionInvalidity,
  loginLocationForCurrentPage,
  parseSessionBootstrap,
  returnTargetFromSearch,
} from "../features/auth/session.js";
import { parsePostgresTaskRoute } from "../features/routing/routes.js";
import { renderLocalPostgresqlAdmin } from "../local/local-admin-app.js";
import { renderLocalPostgresqlBoard } from "../local/local-board-app.js";
import { createApplicationFrame } from "../shell/application-shell.js";
import { renderLoginShell, renderLogoutPendingShell } from "../shell/login-shell.js";

function navigateToLogin(root) {
  root.hidden = true;
  window.location.replace(loginLocationForCurrentPage());
}

function installLogout(root, lifecycle, client, { stopProtectedReads = () => {} } = {}) {
  const header = root.querySelector(".app-header");
  if (!header) return;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "button button--quiet";
  button.textContent = "Sign out";
  header.append(button);
  button.addEventListener("click", () => {
    const csrf = lifecycle.state?.csrfProof;
    if (!csrf) {
      navigateToLogin(root);
      return;
    }
    stopProtectedReads();
    lifecycle.conceal();
    lifecycle.signalLogoutStart();
    lifecycle.stop();
    const attempt = async () => {
      try {
        await client.logout(csrf);
        window.location.replace("/login");
      } catch (error) {
        if (error instanceof FrontendApiError && ["auth_required", "session_expired", "session_revoked"].includes(error.code)) {
          window.location.replace("/login");
          return;
        }
        root.hidden = false;
        renderLogoutPendingShell(root, { onRetry: attempt });
      }
    };
    void attempt();
  });
}

async function bootLogin(root, client) {
  const target = returnTargetFromSearch(window.location.search);
  const render = (message = "", retryAfterSeconds = null) => {
    renderLoginShell(root, {
      message,
      retryAfterSeconds,
      onSubmit: async (password) => {
        renderLoginShell(root, { pending: true, message: "Signing in…" });
        try {
          await client.login(password);
          const startedWall = Date.now();
          const startedMono = performance.now();
          const state = parseSessionBootstrap(await client.session(), {
            wallNow: startedWall,
            monotonicNow: startedMono,
          });
          if (performance.now() >= state.concealAt) throw new Error("session expired before navigation");
          if (typeof BroadcastChannel === "function") {
            const channel = new BroadcastChannel("dish-frontend-session-v1");
            channel.postMessage({ type: "session-change" });
            channel.close();
          }
          window.location.replace(target);
        } catch (error) {
          if (error instanceof FrontendApiError && error.code === "login_throttled") {
            render(error.message, error.retryAfterSeconds);
          } else if (error instanceof FrontendApiError && error.code === "login_invalid") {
            render(error.message);
          } else {
            render("Sign in is temporarily unavailable. Try again.");
          }
        }
      },
    });
  };
  render();
}

function renderReadsDisabled(root) {
  const { shell, main } = createApplicationFrame({ environmentLabel: "READS NOT ACTIVATED" });
  const section = document.createElement("section");
  section.className = "empty-shell";
  const content = document.createElement("div");
  const heading = document.createElement("h2");
  heading.textContent = "Frontend data reads are not activated";
  const description = document.createElement("p");
  description.className = "muted";
  description.textContent = "Authentication is available, but this environment is not exposing the read-only task board.";
  content.append(heading, description);
  section.append(content);
  main.append(section);
  root.replaceChildren(shell);
  root.hidden = false;
  root.dataset.shellState = "private-reads-disabled";
}

export async function bootPrivateFrontend(root, { mode, fetchImpl = globalThis.fetch } = {}) {
  const client = new FrontendHttpClient({ fetchImpl });
  if (window.location.pathname === "/login") {
    await bootLogin(root, client);
    return;
  }

  root.hidden = true;
  const lifecycle = new PrivateSessionLifecycle(root, client);
  try {
    await lifecycle.establish({ conceal: true });
  } catch {
    lifecycle.stop();
    navigateToLogin(root);
    return;
  }
  lifecycle.start();

  const onAuthenticationLost = (error) => {
    if (!isSessionInvalidity(error) && !(error instanceof FrontendApiError && error.code === "session_unavailable")) return false;
    lifecycle.stop();
    navigateToLogin(root);
    return true;
  };

  let protectedController = null;
  if (mode === "private-postgresql" || mode === "private-postgresql-authority") {
    const environmentLabel = mode === "private-postgresql-authority"
      ? "POSTGRESQL — AUTHORITATIVE SOURCE"
      : "POSTGRESQL — NON-AUTHORITATIVE";
    if (window.location.pathname === "/admin") {
      protectedController = await renderLocalPostgresqlAdmin(root, {
        environmentLabel,
        onAuthenticationLost,
      });
    } else {
      protectedController = await renderLocalPostgresqlBoard(root, {
        initialTaskId: parsePostgresTaskRoute(window.location.pathname)?.taskId ?? null,
        environmentLabel,
        onAuthenticationLost,
      });
    }
  } else {
    renderReadsDisabled(root);
  }
  if (!root.hidden) installLogout(root, lifecycle, client, {
    stopProtectedReads: () => protectedController?.stop(),
  });
}
