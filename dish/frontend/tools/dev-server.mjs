import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { createServer } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

const mimeTypes = new Map([
  [".css", "text/css; charset=utf-8"],
  [".html", "text/html; charset=utf-8"],
  [".js", "text/javascript; charset=utf-8"],
  [".json", "application/json; charset=utf-8"],
  [".svg", "image/svg+xml"],
]);

export async function startStaticServer({ root, port = 0 } = {}) {
  const resolvedRoot = path.resolve(root);
  const server = createServer(async (request, response) => {
    const url = new URL(request.url ?? "/", "http://127.0.0.1");
    const requestedPath = url.pathname === "/" ? "/index.html" : url.pathname;
    const candidate = path.resolve(resolvedRoot, `.${requestedPath}`);
    if (!candidate.startsWith(`${resolvedRoot}${path.sep}`)) {
      response.writeHead(404).end("Not found");
      return;
    }
    try {
      const info = await stat(candidate);
      if (!info.isFile()) throw new Error("Not a file");
      response.writeHead(200, {
        "Content-Type": mimeTypes.get(path.extname(candidate)) ?? "application/octet-stream",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
      });
      createReadStream(candidate).pipe(response);
    } catch {
      if (request.method === "GET" && !path.extname(requestedPath)) {
        response.writeHead(200, {
          "Content-Type": "text/html; charset=utf-8",
          "Cache-Control": "no-store",
          "X-Content-Type-Options": "nosniff",
        });
        createReadStream(path.join(resolvedRoot, "index.html")).pipe(response);
        return;
      }
      response.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" }).end("Not found");
    }
  });
  await new Promise((resolve) => server.listen(port, "127.0.0.1", resolve));
  return {
    origin: `http://127.0.0.1:${server.address().port}`,
    close: () => new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve())),
  };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const root = process.env.DISH_FRONTEND_STATIC_ROOT
    ? path.resolve(process.env.DISH_FRONTEND_STATIC_ROOT)
    : path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "dist");
  const server = await startStaticServer({ root, port: Number(process.env.PORT ?? 4173) });
  console.log(`Dish frontend: ${server.origin}`);
}
