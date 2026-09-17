/**
 * Client site config (deploy-time only).
 *
 * - apiToken: Bearer for /v1 (must match server API_KEYS). Not editable in UI.
 * - tenantId: X-Tenant-Id for all business APIs. Not editable in UI.
 *
 * @module shared/config
 */
window.__AFS_CONFIG__ = Object.assign(
  {
    productName: "Agent File Service",
    apiBase: "/v1",
    /**
     * Deploy-time API Bearer (local demo default only).
     * Production: leave console off (DEV_CONSOLE_ENABLED=false), or inject via
     * entrypoint DEV_CONSOLE_API_TOKEN / volume — never bake real secrets into the image.
     */
    apiToken: "dev-api-key",
    /** Deploy-time tenant for X-Tenant-Id (override with DEV_CONSOLE_TENANT_ID) */
    tenantId: "agent-system-a",
    pageSize: 20,
    cacheVersion: "20260820a",
  },
  window.__AFS_CONFIG__ || {},
);

export function getConfig() {
  return window.__AFS_CONFIG__ || {};
}
