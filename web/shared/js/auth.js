/**
 * Bearer + tenant from deploy config only (not user-editable).
 * @module js/auth
 */

import { getConfig } from "../config.js";

/** Read token from shared/config.js (injected at deploy). */
export function getApiKey() {
  const cfg = getConfig();
  return String(cfg.apiToken || "").trim();
}

/** Read tenant id from shared/config.js. */
export function getTenantId() {
  const cfg = getConfig();
  return String(cfg.tenantId || "").trim();
}
