import { FrontendApiError, FrontendContractMismatch } from "../../api/http-transport.js";
import { BoardContractMismatch } from "../board/api-board-model.js";
import { DetailContractMismatch } from "../detail/api-detail-model.js";
import { noticeRegistry } from "../notices/notice-registry.js";

export function refreshErrorMessage(error) {
  if (error instanceof FrontendApiError && error.code === "client_update_required") {
    return "The frontend and server contracts changed. Reload this page before continuing.";
  }
  if (error instanceof FrontendContractMismatch || error instanceof BoardContractMismatch || error instanceof DetailContractMismatch) {
    return "The PostgreSQL response no longer matches this frontend. Reload the page before continuing.";
  }
  if (error instanceof FrontendApiError) return error.message;
  return "The PostgreSQL frontend is temporarily unavailable.";
}

export function refreshFailureNotice(error, { fallbackCode = "service_unavailable", action = "retry" } = {}) {
  if (error instanceof FrontendContractMismatch || error instanceof BoardContractMismatch || error instanceof DetailContractMismatch) {
    return { code: "contract_mismatch", message: refreshErrorMessage(error), action: "reload" };
  }
  const code = error instanceof FrontendApiError && noticeRegistry[error.code] ? error.code : fallbackCode;
  return {
    code,
    message: refreshErrorMessage(error),
    action: code === "client_update_required" ? "reload" : action,
  };
}
