import { FRONTEND_CONTRACT_VERSION } from "../config.js";

export class FrontendApiTransport {
  constructor({ baseUrl = "", fetchImpl = globalThis.fetch } = {}) {
    this.baseUrl = baseUrl;
    this.fetchImpl = fetchImpl;
  }

  async request({ path, method, body, headers = {}, query = null }) {
    const search = query ? new URLSearchParams(query).toString() : "";
    const requestPath = search ? `${path}?${search}` : path;
    const response = await Reflect.apply(this.fetchImpl, globalThis, [`${this.baseUrl}${requestPath}`, {
      method,
      credentials: "same-origin",
      redirect: "manual",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-Dish-Frontend-Contract": FRONTEND_CONTRACT_VERSION,
        ...headers,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    }]);
    if (response.type === "opaqueredirect" || response.redirected) {
      throw new Error("Frontend API redirects are rejected");
    }
    return response;
  }
}
