import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const schema = JSON.parse(await readFile(path.join(root, "openapi", "frontend.openapi.json"), "utf8"));
const errors = [];

if (schema.openapi !== "3.1.0") errors.push("OpenAPI version must be 3.1.0");
if (schema.info?.version !== "dish-frontend-v1") errors.push("Contract version must be dish-frontend-v1");
if (!schema.components?.securitySchemes?.frontendSession) errors.push("Cookie session scheme is missing");
const operationIds = new Set();
for (const [route, pathItem] of Object.entries(schema.paths ?? {})) {
  for (const method of ["get", "post", "put", "patch", "delete"]) {
    const operation = pathItem[method];
    if (!operation) continue;
    if (!operation.operationId) errors.push(`${method.toUpperCase()} ${route} has no operationId`);
    if (operationIds.has(operation.operationId)) errors.push(`Duplicate operationId ${operation.operationId}`);
    operationIds.add(operation.operationId);
    if (!operation.responses) errors.push(`${operation.operationId} has no responses`);
  }
}
for (const required of ["frontendLogin", "getFrontendSession", "frontendLogout", "getFrontendBoard", "getFrontendArchive", "getFrontendSectionTasks", "getFrontendTaskDetail"]) {
  if (!operationIds.has(required)) errors.push(`Required operation ${required} is missing`);
}
if (errors.length) {
  console.error(errors.join("\n"));
  process.exit(1);
}
console.log(`OpenAPI contract checked: ${operationIds.size} operations`);
