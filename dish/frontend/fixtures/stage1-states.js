export const lifecycleFixtures = Object.freeze({
  loading: {
    kind: "loading",
    message: "Loading the fixture board…",
  },
  initialError: {
    kind: "initial-error",
    code: "initial_load_failed",
    message: "The board could not be loaded. The persistent shell is still available.",
  },
  lastSafe: {
    kind: "last-safe",
    code: "service_unavailable",
    message: "Refresh failed. Showing the last successful fixture board.",
  },
});
