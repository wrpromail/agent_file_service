/**
 * Agent File Service API client (Bearer + X-Tenant-Id).
 * @module js/api
 */

import { getConfig } from "../config.js";
import { getApiKey, getTenantId } from "./auth.js";
import { identityHeaders, noteUcUnauthorized } from "./uc.js";
import { buildQuery } from "./utils.js";

function apiBase() {
  return (getConfig().apiBase || "/v1").replace(/\/$/, "");
}

/**
 * @param {unknown} body
 * @param {number} status
 */
function extractError(body, status) {
  if (body && typeof body === "object") {
    const o = /** @type {Record<string, unknown>} */ (body);
    if (typeof o.detail === "string") return o.detail;
    if (typeof o.error === "string") return o.error;
    if (o.detail != null) {
      try {
        return JSON.stringify(o.detail);
      } catch {
        /* fallthrough */
      }
    }
  }
  if (typeof body === "string" && body) return body.slice(0, 400);
  return `HTTP ${status}`;
}

/**
 * @param {string} path
 * @param {RequestInit & { raw?: boolean, query?: Record<string, unknown>, skipAuth?: boolean }} [opts]
 */
export async function api(path, opts = {}) {
  const { raw, query, skipAuth, ...init } = opts;
  let url = path.startsWith("http")
    ? path
    : path.startsWith("/v1") || path.startsWith("/health") || path.startsWith("/metrics")
      ? path
      : `${apiBase()}${path.startsWith("/") ? path : `/${path}`}`;
  if (query) url += buildQuery(/** @type {Record<string, string|number|boolean>} */ (query));

  const headers = new Headers(init.headers || {});
  if (!skipAuth) {
    const key = getApiKey();
    const tenant = getTenantId();
    if (key && !headers.has("Authorization")) {
      headers.set("Authorization", `Bearer ${key}`);
    }
    if (tenant && !headers.has("X-Tenant-Id")) {
      headers.set("X-Tenant-Id", tenant);
    }
    for (const [k, v] of Object.entries(identityHeaders())) {
      if (!headers.has(k)) headers.set(k, v);
    }
  }
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const res = await fetch(url, { ...init, headers });
  await noteUcUnauthorized(res);
  if (raw) return res;

  const ct = res.headers.get("content-type") || "";
  const body = ct.includes("application/json") ? await res.json() : await res.text();
  if (!res.ok) {
    const err = new Error(extractError(body, res.status));
    // @ts-expect-error attach status
    err.status = res.status;
    // @ts-expect-error attach body
    err.body = body;
    throw err;
  }
  return body;
}

/** Public (no auth) */
export async function fetchPublic(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return res.json();
  return res.text();
}

export async function getHealth(deep = false) {
  return fetchPublic(deep ? "/health?deep=true" : "/health");
}

export async function getMetricsText() {
  const res = await fetch("/metrics");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.text();
}

// ── Files API ──────────────────────────────────────────────────────────

/** @param {Record<string, unknown>} [query] */
export function listFiles(query = {}) {
  return api("/files", { query });
}

/** @param {string} id */
export function getFile(id) {
  return api(`/files/${id}`);
}

/** @param {string} id @param {string|null} [profile] */
export function getFileStatus(id, profile = null) {
  return api(`/files/${id}/status`, { query: profile ? { profile } : {} });
}

/**
 * @param {string[]} referenceIds
 * @param {string|null} [profile]
 */
export function batchStatus(referenceIds, profile = null) {
  return api("/files/batch-status", {
    method: "POST",
    body: JSON.stringify({
      reference_ids: referenceIds,
      profile: profile || undefined,
    }),
  });
}

/**
 * @param {string} id
 * @param {Record<string, unknown>} [query]
 */
export function getFileContent(id, query = {}) {
  return api(`/files/${id}/content`, { query });
}

/**
 * @param {string} id
 * @param {{ profile?: string, force?: boolean }} body
 */
export function triggerParse(id, body) {
  return api(`/files/${id}/parse`, {
    method: "POST",
    body: JSON.stringify({
      profile: body.profile || "layout",
      force: !!body.force,
    }),
  });
}

/**
 * @param {string} id
 * @param {{ prompt: string, media_kind?: string|null, created_by?: string|null }} body
 */
export function createCustomParse(id, body) {
  return api(`/files/${id}/custom-parse`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** @param {string} id @param {Record<string, unknown>} [query] */
export function listCustomParses(id, query = {}) {
  return api(`/files/${id}/custom-parse`, { query });
}

/** @param {string} id @param {string} jobId */
export function getCustomParse(id, jobId) {
  return api(`/files/${id}/custom-parse/${jobId}`);
}

/** @param {string} id */
export function deleteFile(id) {
  return api(`/files/${id}`, { method: "DELETE" });
}

/** @param {string} id @param {string|null} [password] */
export function getArchiveMembers(id, password = null) {
  return api(`/files/${id}/archive/members`, {
    query: password ? { archive_password: password } : {},
  });
}

/**
 * Multipart upload.
 * @param {File} file
 * @param {Record<string, string|boolean|undefined|null>} fields
 */
export async function uploadFile(file, fields = {}) {
  const fd = new FormData();
  fd.append("file", file);
  for (const [k, v] of Object.entries(fields)) {
    if (v === undefined || v === null || v === "") continue;
    fd.append(k, String(v));
  }
  return api("/files", { method: "POST", body: fd });
}

/** @param {Record<string, unknown>} body */
export function uploadFromUrl(body) {
  return api("/files/from-url", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/**
 * @param {string} sessionId
 * @param {Record<string, unknown>} [query]
 */
export function listSessionFiles(sessionId, query = {}) {
  return api(`/sessions/${encodeURIComponent(sessionId)}/files`, { query });
}

/**
 * Download original file (triggers browser save).
 * @param {string} id
 * @param {string} [filename]
 */
export async function downloadFile(id, filename = "download") {
  const res = /** @type {Response} */ (
    await api(`/files/${id}/download`, { raw: true })
  );
  if (!res.ok) {
    const t = await res.text();
    throw new Error(t || `HTTP ${res.status}`);
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename || "download";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/**
 * Low-level request for playground (returns timing + status).
 * @param {{ method: string, path: string, query?: Record<string,unknown>, body?: string|null, formData?: FormData|null }} req
 */
export async function playgroundRequest(req) {
  const method = (req.method || "GET").toUpperCase();
  /** @type {RequestInit & { raw?: boolean, query?: Record<string, unknown> }} */
  const opts = { method, raw: true, query: req.query };
  if (req.formData) {
    opts.body = req.formData;
  } else if (req.body != null && req.body !== "" && method !== "GET" && method !== "HEAD") {
    opts.body = req.body;
    opts.headers = { "Content-Type": "application/json" };
  }
  const started = performance.now();
  const res = /** @type {Response} */ (await api(req.path, opts));
  const elapsedMs = Math.round(performance.now() - started);
  const ct = res.headers.get("content-type") || "";
  let body;
  if (ct.includes("application/json")) {
    try {
      body = await res.json();
    } catch {
      body = await res.text();
    }
  } else {
    body = await res.text();
  }
  return {
    ok: res.ok,
    status: res.status,
    elapsedMs,
    headers: Object.fromEntries(res.headers.entries()),
    body,
  };
}
