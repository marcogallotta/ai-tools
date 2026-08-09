import assert from "node:assert/strict";
import test from "node:test";
import {
  PrivateSessionLifecycle,
  SessionContractMismatch,
  loginLocationForCurrentPage,
  parseSessionBootstrap,
  returnTargetFromSearch,
} from "../../src/js/features/auth/session.js";

function encodeTarget(target) {
  const bytes = new TextEncoder().encode(target);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

test("session concealment uses the earlier absolute or request-relative boundary", () => {
  const wallNow = Date.parse("2026-08-09T08:00:00Z");
  const parsed = parseSessionBootstrap({
    expires_at: "2026-08-09T08:10:00Z",
    remaining_seconds: 120,
    csrf_proof: "A".repeat(43),
  }, { wallNow, monotonicNow: 5000 });
  assert.equal(parsed.concealAt, 125000);
  assert.equal(parsed.csrfProof, "A".repeat(43));
});

test("session bootstrap rejects extra fields and unsafe CSRF material", () => {
  assert.throws(() => parseSessionBootstrap({
    expires_at: "2026-08-09T08:10:00Z",
    remaining_seconds: 120,
    csrf_proof: "A".repeat(43),
    extra: true,
  }), SessionContractMismatch);
  assert.throws(() => parseSessionBootstrap({
    expires_at: "2026-08-09T08:10:00Z",
    remaining_seconds: 120,
    csrf_proof: "bad\nproof".repeat(4),
  }), SessionContractMismatch);
});

test("opaque login return targets restore only approved same-origin application routes", () => {
  const previous = globalThis.window;
  globalThis.window = { location: { origin: "https://dish.example.test", pathname: "/", search: "" } };
  try {
    const task = "/tasks/r1t-AAAAAAAAAAAAAAAAAAAAAAAAAAA/current-title";
    assert.equal(returnTargetFromSearch(`?return=rt1.${encodeTarget(task)}`), task);
    assert.equal(returnTargetFromSearch(`?return=rt1.${encodeTarget("https://evil.example/")}`), "/");
    assert.equal(returnTargetFromSearch(`?return=rt1.${encodeTarget("/frontend/session")}`), "/");
    assert.equal(returnTargetFromSearch(`?return=rt1.${encodeTarget(`${task}?x=1`)}`), "/");
    assert.equal(returnTargetFromSearch(`?return=rt1.${encodeTarget("/tasks/../frontend/session")}`), "/");
    assert.equal(returnTargetFromSearch("?return=rt1.bad&return=rt1.other"), "/");
  } finally {
    globalThis.window = previous;
  }
});

test("current protected location becomes an opaque login return token", () => {
  const previous = globalThis.window;
  globalThis.window = {
    location: {
      origin: "https://dish.example.test",
      pathname: "/tasks/r1t-AAAAAAAAAAAAAAAAAAAAAAAAAAA/current-title",
      search: "?x=1",
    },
  };
  try {
    const location = loginLocationForCurrentPage();
    assert.match(location, /^\/login\?return=rt1\.[A-Za-z0-9_-]+$/);
    assert.equal(returnTargetFromSearch(location.slice("/login".length)), "/tasks/r1t-AAAAAAAAAAAAAAAAAAAAAAAAAAA/current-title");
  } finally {
    globalThis.window = previous;
  }
});


test("session replacement conceals and reloads instead of accepting stale protected responses", () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousBroadcastChannel = globalThis.BroadcastChannel;
  let listener = null;
  let reloads = 0;
  let sessionCalls = 0;
  globalThis.BroadcastChannel = class {};
  globalThis.window = {
    location: { reload() { reloads += 1; } },
    addEventListener() {},
    removeEventListener() {},
  };
  globalThis.document = {
    visibilityState: "visible",
    addEventListener() {},
    removeEventListener() {},
  };
  const root = { hidden: false };
  const lifecycle = new PrivateSessionLifecycle(root, { async session() { sessionCalls += 1; } }, {
    channelFactory: () => ({
      addEventListener(_name, fn) { listener = fn; },
      close() {},
      postMessage() {},
    }),
  });
  try {
    listener({ data: { type: "session-change" } });
    assert.equal(root.hidden, true);
    assert.equal(sessionCalls, 0);
    assert.equal(reloads, 1);
  } finally {
    lifecycle.stop();
    globalThis.window = previousWindow;
    globalThis.document = previousDocument;
    globalThis.BroadcastChannel = previousBroadcastChannel;
  }
});


test("logout-start signal conceals other tabs without revalidating the old session", () => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  const previousBroadcastChannel = globalThis.BroadcastChannel;
  let listener = null;
  let sessionCalls = 0;
  let replaced = null;
  globalThis.BroadcastChannel = class {};
  globalThis.window = {
    location: {
      replace(value) { replaced = value; },
    },
    addEventListener() {},
    removeEventListener() {},
  };
  globalThis.document = {
    visibilityState: "visible",
    addEventListener() {},
    removeEventListener() {},
  };
  const channel = {
    addEventListener(_type, callback) { listener = callback; },
    postMessage() {},
    close() {},
  };
  const root = { hidden: false };
  try {
    new PrivateSessionLifecycle(root, {
      async session() { sessionCalls += 1; return {}; },
    }, { channelFactory: () => channel });
    listener({ data: { type: "logout-start" } });
    assert.equal(root.hidden, true);
    assert.equal(replaced, "/login");
    assert.equal(sessionCalls, 0);
  } finally {
    globalThis.window = previousWindow;
    globalThis.document = previousDocument;
    globalThis.BroadcastChannel = previousBroadcastChannel;
  }
});
