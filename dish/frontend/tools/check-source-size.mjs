import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { walkFiles } from "./files.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const includedRoots = ["src", "tools", "tests"].map((name) => path.join(root, name));
const extensions = new Set([".js", ".mjs", ".css", ".html"]);
const reviewThreshold = 250;
const hardLimit = 350;
const failures = [];
const reviews = [];

for (const includedRoot of includedRoots) {
  for (const file of await walkFiles(includedRoot)) {
    if (!extensions.has(path.extname(file))) continue;
    if (file.includes(`${path.sep}generated${path.sep}`)) continue;
    const logicalLines = (await readFile(file, "utf8")).split("\n").filter((line) => line.trim()).length;
    const relative = path.relative(root, file);
    if (logicalLines > hardLimit) failures.push(`${relative}: ${logicalLines} logical lines exceeds ${hardLimit}`);
    else if (logicalLines > reviewThreshold) reviews.push(`${relative}: ${logicalLines} logical lines exceeds review threshold ${reviewThreshold}`);
  }
}
if (reviews.length) console.warn(reviews.join("\n"));
if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
