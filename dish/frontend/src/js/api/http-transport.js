import { GeneratedFrontendClient } from "./generated/frontend-client.js";
import { FrontendApiTransport } from "./transport.js";
import { FRONTEND_CONTRACT_VERSION } from "../config.js";

const SERVER_ERROR_CODES = new Set([
  "auth_required",
  "session_expired",
  "session_revoked",
  "session_unavailable",
  "logout_unavailable",
  "login_invalid",
  "login_throttled",
  "origin_rejected",
  "csrf_rejected",
  "media_type_unsupported",
  "request_invalid",
  "board_configuration_invalid",
  "task_not_found",
  "task_ineligible",
  "cursor_invalid",
  "cursor_stale",
  "client_update_required",
  "service_unavailable",
  "board_capacity_exceeded",
  "detail_capacity_exceeded",
  "internal_error",
]);

const BOARD_ERROR_CODES = Object.freeze({
  403: new Set(["client_update_required"]),
  503: new Set([
    "board_configuration_invalid",
    "board_capacity_exceeded",
    "service_unavailable",
    "internal_error",
  ]),
});

const SECTION_ERROR_CODES = Object.freeze({
  400: new Set(["request_invalid", "cursor_invalid"]),
  403: new Set(["client_update_required"]),
  409: new Set(["cursor_stale"]),
  503: new Set([
    "board_configuration_invalid",
    "board_capacity_exceeded",
    "service_unavailable",
    "internal_error",
  ]),
});

export class FrontendContractMismatch extends Error {
  constructor(message = "Frontend response contract mismatch") {
    super(message);
    this.name = "FrontendContractMismatch";
    this.code = "contract_mismatch";
  }
}

export class FrontendApiError extends Error {
  constructor({ status, code, message, retryAfterSeconds = null }) {
    super(message);
    this.name = "FrontendApiError";
    this.status = status;
    this.code = code;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

function contractHeader(response) {
  const value = response.headers.get("X-Dish-Frontend-Contract");
  if (!value || value.includes(",") || value !== FRONTEND_CONTRACT_VERSION) {
    throw new FrontendContractMismatch();
  }
}

function validateErrorEnvelope(payload, status, errorCodesByStatus) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new FrontendContractMismatch();
  if (Object.keys(payload).length !== 1 || !payload.error || typeof payload.error !== "object") {
    throw new FrontendContractMismatch();
  }
  const error = payload.error;
  const allowedKeys = new Set(["code", "message", "retry_after_seconds"]);
  if (Object.keys(error).some((key) => !allowedKeys.has(key))) throw new FrontendContractMismatch();
  if (!SERVER_ERROR_CODES.has(error.code)) throw new FrontendContractMismatch();
  const allowedForStatus = errorCodesByStatus?.[status];
  if (!allowedForStatus || !allowedForStatus.has(error.code)) throw new FrontendContractMismatch();
  if (typeof error.message !== "string" || error.message.length < 1 || error.message.length > 240) {
    throw new FrontendContractMismatch();
  }
  if (error.retry_after_seconds !== undefined) {
    if (error.code !== "login_throttled"
      || !Number.isInteger(error.retry_after_seconds)
      || error.retry_after_seconds < 0
      || error.retry_after_seconds > 3600) {
      throw new FrontendContractMismatch();
    }
  }
  return new FrontendApiError({
    status,
    code: error.code,
    message: error.message,
    retryAfterSeconds: error.retry_after_seconds ?? null,
  });
}

export async function readFrontendJson(response, { errorCodesByStatus = null } = {}) {
  contractHeader(response);
  const contentType = response.headers.get("Content-Type") ?? "";
  const mediaType = contentType.split(";", 1)[0].trim().toLowerCase();
  if (mediaType !== "application/json") throw new FrontendContractMismatch();
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new FrontendContractMismatch();
  }
  if (response.ok) {
    if (response.status !== 200) throw new FrontendContractMismatch();
    return payload;
  }
  throw validateErrorEnvelope(payload, response.status, errorCodesByStatus);
}

export class FrontendHttpClient {
  constructor({ fetchImpl = globalThis.fetch } = {}) {
    const transport = new FrontendApiTransport({ fetchImpl });
    this.client = new GeneratedFrontendClient(transport);
  }

  async board() {
    return readFrontendJson(await this.client.getFrontendBoard(), {
      errorCodesByStatus: BOARD_ERROR_CODES,
    });
  }

  async sectionTasks(sectionId, cursor) {
    return readFrontendJson(await this.client.getFrontendSectionTasks(sectionId, {
      query: { cursor },
    }), { errorCodesByStatus: SECTION_ERROR_CODES });
  }
}
