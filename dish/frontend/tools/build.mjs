import { access, cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const reviewBuild = process.argv.includes("--review");
const generator = spawnSync(process.execPath, [path.join(root, "tools", "generate-client.mjs")], { stdio: "inherit" });
if (generator.status !== 0) process.exit(generator.status ?? 1);

const dist = path.join(root, reviewBuild ? "review-dist" : "dist");
await rm(dist, { recursive: true, force: true });
await mkdir(dist, { recursive: true });
await cp(path.join(root, "src"), dist, { recursive: true });

if (reviewBuild) {
  await cp(path.join(root, "fixtures"), path.join(dist, "fixtures"), { recursive: true });
  const indexPath = path.join(dist, "index.html");
  const index = await readFile(indexPath, "utf-8");
  await writeFile(indexPath, index
    .replace('name="dish-runtime-mode" content="local-observation"', 'name="dish-runtime-mode" content="fixture-review"')
    .replace('    <script type="module" src="/js/boot.js"></script>', '    <link rel="stylesheet" href="/styles/review.css">\n    <script type="module" src="/js/review/review-boot.js"></script>'));
  await writeFile(path.join(dist, "build.json"), `${JSON.stringify({
    contractVersion: "dish-frontend-v1",
    fixtureBacked: true,
    networkMode: "fixture-review-only",
    reviewModeNetwork: "fixture-only",
    reviewCatalogue: true,
  }, null, 2)}\n`);
  console.log(`Built fixture review frontend at ${dist}`);
} else {
  await rm(path.join(dist, "js", "prototype"), { recursive: true, force: true });
  await rm(path.join(dist, "js", "review"), { recursive: true, force: true });
  await rm(path.join(dist, "styles", "review.css"), { force: true });
  await writeFile(path.join(dist, "build.json"), `${JSON.stringify({
    contractVersion: "dish-frontend-v1",
    fixtureBacked: false,
    networkMode: "read-only-postgresql",
    localPostgresqlObservation: true,
    privateAuthenticationCandidate: true,
    privatePostgresqlReadsExplicitActivation: true,
  }, null, 2)}\n`);

  for (const forbidden of ["fixtures", "js/prototype", "js/review", "styles/review.css"]) {
    try {
      await access(path.join(dist, forbidden));
      throw new Error(`Production frontend contains forbidden review artifact: ${forbidden}`);
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
  }
  console.log(`Built production-shaped frontend at ${dist}`);
}
