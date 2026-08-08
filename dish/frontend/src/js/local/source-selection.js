export function frontendDataSource(search = "") {
  const parameters = new URLSearchParams(search);
  if (parameters.getAll("review").includes("1")) return "fixture";
  const sources = parameters.getAll("source");
  return sources.length === 1 && sources[0] === "postgresql" ? "postgresql" : "fixture";
}
