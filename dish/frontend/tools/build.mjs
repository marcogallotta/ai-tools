import { cp, mkdir, rm, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const generator = spawnSync(process.execPath, [path.join(root, "tools", "generate-client.mjs")], { stdio: "inherit" });
if (generator.status !== 0) process.exit(generator.status ?? 1);
const dist = path.join(root, "dist");
await rm(dist, { recursive: true, force: true });
await mkdir(dist, { recursive: true });
await cp(path.join(root, "src"), dist, { recursive: true });
await cp(path.join(root, "fixtures"), path.join(dist, "fixtures"), { recursive: true });
await writeFile(path.join(dist, "build.json"), `${JSON.stringify({ contractVersion: "dish-frontend-v1", fixtureBacked: true }, null, 2)}\n`);
console.log(`Built frontend at ${dist}`);
