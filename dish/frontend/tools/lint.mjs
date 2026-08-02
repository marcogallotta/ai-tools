import { readFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { walkFiles } from "./files.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const files = (await walkFiles(root)).filter((file) => !file.includes(`${path.sep}dist${path.sep}`));
const textExtensions = new Set([".js", ".mjs", ".css", ".html", ".json", ".md"]);
const errors = [];
for (const file of files) {
  if (!textExtensions.has(path.extname(file))) continue;
  const relative = path.relative(root, file);
  const content = await readFile(file, "utf8");
  if (!content.endsWith("\n")) errors.push(`${relative}: missing final newline`);
  content.split("\n").forEach((line, index) => {
    if (/\s+$/.test(line)) errors.push(`${relative}:${index + 1}: trailing whitespace`);
    if (line.includes("\t")) errors.push(`${relative}:${index + 1}: tab character`);
  });
  if (path.extname(file) === ".json") {
    try { JSON.parse(content); } catch (error) { errors.push(`${relative}: ${error.message}`); }
  }
  if (path.extname(file) === ".html") {
    if (/<style[\s>]/i.test(content)) errors.push(`${relative}: embedded style is forbidden`);
    if (/<script(?![^>]*\bsrc=)[^>]*>/i.test(content)) errors.push(`${relative}: embedded script is forbidden`);
  }
}
if (!process.argv.includes("--format-only")) {
  for (const file of files.filter((item) => [".js", ".mjs"].includes(path.extname(item)))) {
    const result = spawnSync(process.execPath, ["--check", file], { encoding: "utf8" });
    if (result.status !== 0) errors.push(`${path.relative(root, file)}: ${result.stderr.trim()}`);
  }
}
if (errors.length) {
  console.error(errors.join("\n"));
  process.exit(1);
}
if (!process.argv.includes("--format-only")) {
  const sizeCheck = spawnSync(process.execPath, [path.join(root, "tools", "check-source-size.mjs")], { stdio: "inherit" });
  if (sizeCheck.status !== 0) process.exit(sizeCheck.status ?? 1);
}
