import { FrontendApiError, FrontendContractMismatch } from "../../api/http-transport.js";

export const SESSION_FEATURE_STATUS = "delivery-stage-2-implementation-candidate";
export const SESSION_REVALIDATE_MS = 25000;
export const SESSION_CHANNEL = "dish-frontend-session-v1";

const SESSION_INVALID_CODES = new Set(["auth_required", "session_expired", "session_revoked"]);
const TASK_PATH = /^\/tasks\/r1t-[A-Za-z0-9_-]{27}\/[^/?#]{1,600}$/;

export class SessionContractMismatch extends Error {
  constructor() {
    super("Frontend session response contract mismatch");
    this.name = "SessionContractMismatch";
  }
}

function exactKeys(value, expected) {
  return value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === expected.length
    && expected.every((key) => Object.hasOwn(value, key));
}

export function parseSessionBootstrap(payload, { wallNow = Date.now(), monotonicNow = performance.now() } = {}) {
  if (!exactKeys(payload, ["expires_at", "remaining_seconds", "csrf_proof"])) throw new SessionContractMismatch();
  const expiresAt = Date.parse(payload.expires_at);
  if (!Number.isFinite(expiresAt)) throw new SessionContractMismatch();
  if (!Number.isInteger(payload.remaining_seconds) || payload.remaining_seconds < 0 || payload.remaining_seconds > 604800) {
    throw new SessionContractMismatch();
  }
  if (typeof payload.csrf_proof !== "string" || payload.csrf_proof.length < 22 || payload.csrf_proof.length > 256
    || [...payload.csrf_proof].some((char) => char.charCodeAt(0) < 0x21 || char.charCodeAt(0) > 0x7e)) {
    throw new SessionContractMismatch();
  }
  const absoluteRemaining = Math.max(0, expiresAt - wallNow);
  const relativeRemaining = payload.remaining_seconds * 1000;
  return Object.freeze({
    expiresAt,
    csrfProof: payload.csrf_proof,
    concealAt: monotonicNow + Math.min(absoluteRemaining, relativeRemaining),
  });
}

export function isSessionInvalidity(error) {
  return error instanceof FrontendApiError && SESSION_INVALID_CODES.has(error.code);
}

export function returnTargetFromSearch(search) {
  const params = new URLSearchParams(search);
  const values = params.getAll("return");
  if (values.length !== 1 || !values[0].startsWith("rt1.")) return "/";
  const encoded = values[0].slice(4);
  if (!encoded || !/^[A-Za-z0-9_-]+$/.test(encoded)) return "/";
  try {
    const padded = encoded + "=".repeat((4 - (encoded.length % 4)) % 4);
    const bytes = Uint8Array.from(atob(padded.replace(/-/g, "+").replace(/_/g, "/")), (char) => char.charCodeAt(0));
    const target = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    return target === "/" || TASK_PATH.test(target) ? target : "/";
  } catch {
    return "/";
  }
}

export function loginLocationForCurrentPage() {
  const pathname = window.location.pathname;
  const target = pathname === "/" || TASK_PATH.test(pathname) ? pathname : "/";
  const bytes = new TextEncoder().encode(target);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  const encoded = btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  return `/login?return=rt1.${encoded}`;
}

export class PrivateSessionLifecycle {
  constructor(root, client, {
    setTimer = globalThis.setTimeout,
    clearTimer = globalThis.clearTimeout,
    channelFactory = (name) => new BroadcastChannel(name),
  } = {}) {
    this.root = root;
    this.client = client;
    this.setTimer = setTimer;
    this.clearTimer = clearTimer;
    this.sequence = 0;
    this.expiryTimer = null;
    this.revalidateTimer = null;
    this.state = null;
    this.channel = typeof BroadcastChannel === "function" ? channelFactory(SESSION_CHANNEL) : null;
    this.channel?.addEventListener("message", (event) => {
      if (event?.data?.type === "logout-start") {
        this.conceal();
        window.location.replace("/login");
      } else if (event?.data?.type === "session-change") {
        this.stop();
        window.location.reload();
      }
    });
    this.onVisibility = () => {
      if (document.visibilityState === "visible") this._revalidateOrLogin(true);
    };
    this.onPageShow = () => { this._revalidateOrLogin(true); };
  }

  async establish({ conceal = true } = {}) {
    return this.revalidate({ conceal });
  }

  async revalidate({ conceal = false } = {}) {
    const sequence = ++this.sequence;
    if (conceal) this.root.hidden = true;
    const requestStartWall = Date.now();
    const requestStartMono = performance.now();
    try {
      const payload = await this.client.session();
      if (sequence !== this.sequence) return false;
      const state = parseSessionBootstrap(payload, { wallNow: requestStartWall, monotonicNow: requestStartMono });
      if (performance.now() >= state.concealAt) throw new SessionContractMismatch();
      this.state = state;
      this._schedule();
      this.root.hidden = false;
      return true;
    } catch (error) {
      if (sequence !== this.sequence) return false;
      this.conceal();
      throw error;
    }
  }

  start() {
    document.addEventListener("visibilitychange", this.onVisibility);
    window.addEventListener("pageshow", this.onPageShow);
    this._scheduleRevalidation();
  }

  conceal() {
    this.sequence += 1;
    this.root.hidden = true;
    this.state = null;
    if (this.expiryTimer !== null) this.clearTimer(this.expiryTimer);
    if (this.revalidateTimer !== null) this.clearTimer(this.revalidateTimer);
    this.expiryTimer = null;
    this.revalidateTimer = null;
  }

  signalSessionChange() {
    this.channel?.postMessage({ type: "session-change" });
  }

  signalLogoutStart() {
    this.channel?.postMessage({ type: "logout-start" });
  }

  stop() {
    this.conceal();
    document.removeEventListener("visibilitychange", this.onVisibility);
    window.removeEventListener("pageshow", this.onPageShow);
    this.channel?.close();
  }

  _schedule() {
    if (this.expiryTimer !== null) this.clearTimer(this.expiryTimer);
    const delay = Math.max(0, Math.min(2147483647, this.state.concealAt - performance.now()));
    this.expiryTimer = this.setTimer(() => {
      this.conceal();
      window.location.replace(loginLocationForCurrentPage());
    }, delay);
    this._scheduleRevalidation();
  }

  _revalidateOrLogin(conceal = false) {
    void this.revalidate({ conceal }).catch(() => {
      window.location.replace(loginLocationForCurrentPage());
    });
  }

  _scheduleRevalidation() {
    if (this.revalidateTimer !== null) this.clearTimer(this.revalidateTimer);
    if (!this.state) return;
    this.revalidateTimer = this.setTimer(() => {
      if (document.visibilityState === "visible") {
        this._revalidateOrLogin(false);
      } else {
        this._scheduleRevalidation();
      }
    }, SESSION_REVALIDATE_MS);
  }
}

export function isLifecycleFailure(error) {
  return isSessionInvalidity(error) || error instanceof FrontendContractMismatch || error instanceof SessionContractMismatch;
}
