import { readdir } from "node:fs/promises";
import path from "node:path";

export async function walkFiles(root) {
  const found = [];
  for (const entry of await readdir(root, { withFileTypes: true })) {
    const fullPath = path.join(root, entry.name);
    if (entry.isDirectory()) {
      found.push(...await walkFiles(fullPath));
    } else {
      found.push(fullPath);
    }
  }
  return found;
}
