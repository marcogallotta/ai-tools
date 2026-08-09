import { FrontendApiError, FrontendHttpClient } from "../api/http-transport.js";
import {
  PrivateSessionLifecycle,
  isLifecycleFailure,
  loginLocationForCurrentPage,
  parseSessionBootstrap,
  returnTargetFromSearch,
} from "../features/auth/session.js";
import { parsePostgresTaskRoute, parseTaskRoute } from "../features/routing/routes.js";
import { renderLocalPostgresqlBoard } from "../local/local-board-app.js";
import { renderFixturePrototype } from "../prototype/prototype-app.js";
import { renderLoginShell, renderLogoutPendingShell } from "../shell/login-shell.js";

function navigateToLogin(root) {
  root.hidden = true;
  window.location.replace(loginLocationForCurrentPage());
}

function installLogout(root, lifecycle, client) {
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
    if (!isLifecycleFailure(error) && !(error instanceof FrontendApiError && error.code === "session_unavailable")) return false;
    lifecycle.stop();
    navigateToLogin(root);
    return true;
  };

  if (mode === "private-postgresql") {
    await renderLocalPostgresqlBoard(root, {
      initialTaskId: parsePostgresTaskRoute(window.location.pathname)?.taskId ?? null,
      prototypeLabel: "POSTGRESQL — NON-AUTHORITATIVE",
      onAuthenticationLost,
    });
  } else {
    renderFixturePrototype(root, "board", parseTaskRoute(window.location.pathname), { reviewMode: false });
  }
  if (!root.hidden) installLogout(root, lifecycle, client);
}
