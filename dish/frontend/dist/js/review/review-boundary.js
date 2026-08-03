const forbiddenApiPrefix = "/api/";

export function installFixtureReviewBoundary(target = window) {
  if (target.document?.documentElement?.dataset.fixtureBoundary === "installed") return;
  const originalFetch = target.fetch?.bind(target);
  if (originalFetch) {
    target.fetch = (input, init) => {
      const base = target.location.origin && target.location.origin !== "null" ? target.location.href : "http://fixture-review.local/";
      const requested = new URL(typeof input === "string" ? input : input.url, base);
      const expectedOrigin = new URL(base).origin;
      if (requested.origin !== expectedOrigin || requested.pathname.startsWith(forbiddenApiPrefix)) {
        return Promise.reject(new Error("Fixture review mode blocks backend and cross-origin requests"));
      }
      return originalFetch(input, init);
    };
  }
  target.document.documentElement.dataset.fixtureBoundary = "installed";
}

export function assertFixtureBuild(buildMetadata) {
  if (buildMetadata?.fixtureBacked !== true || buildMetadata?.networkMode !== "fixture-only") {
    throw new Error("Review mode requires an explicitly fixture-only build");
  }
}
