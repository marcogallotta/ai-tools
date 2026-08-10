export function frontendDataSource(search = "") {
  const parameters = new URLSearchParams(search);
  const sources = parameters.getAll("source");
  return sources.length === 1 && sources[0] === "postgresql" ? "postgresql" : null;
}
