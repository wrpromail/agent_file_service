/**
 * UC 工作台跳转：剥 ?token=、换票、会话头、401 清票。
 * 浏览器只持本地 HMAC 票（X-UC-Session），平台 JWT 不进 Authorization。
 * @module js/uc
 */

export const UC_SESSION_KEY = "afs_uc_session";
export const UC_HANDOFF_TOKEN_KEY = "afs_uc_handoff_token";
export const UC_EXPIRED_EVENT = "afs-uc-session-expired";

/**
 * @param {string} [primary]
 * @returns {string[]}
 */
export function workbenchTokenKeys(primary) {
  const p = String(primary || "token").trim() || "token";
  const keys = [p];
  for (const extra of ["token", "workbench_token"]) {
    if (!keys.includes(extra)) keys.push(extra);
  }
  return keys;
}

/**
 * @param {string} href
 * @param {string[]} keys
 * @returns {{ token: string, path: string }}
 */
export function parseAndStripWorkbenchToken(href, keys) {
  const url = new URL(String(href || ""), "http://local.invalid");
  const keyList = keys.length ? keys : workbenchTokenKeys("token");
  let token = "";

  for (const key of keyList) {
    const t = (url.searchParams.get(key) || "").trim();
    if (t && !token) token = t;
    url.searchParams.delete(key);
  }

  let hash = url.hash || "";
  if (hash.includes("?")) {
    const qIndex = hash.indexOf("?");
    const pathPart = hash.slice(1, qIndex);
    const hp = new URLSearchParams(hash.slice(qIndex + 1));
    for (const key of keyList) {
      const t = (hp.get(key) || "").trim();
      if (t && !token) token = t;
      hp.delete(key);
    }
    const rest = hp.toString();
    if (rest) hash = `#${pathPart}?${rest}`;
    else if (pathPart && pathPart !== "/") hash = `#${pathPart}`;
    else hash = "";
  }

  return { token, path: `${url.pathname}${url.search}${hash}` };
}

/**
 * @param {string} [pathname]
 */
export function isUcAuthPath(pathname) {
  const here = String(pathname || "/").split("?")[0].split("#")[0] || "/";
  return here === "/auth" || here === "/auth/" || here === "/auth/index.html";
}

/**
 * @param {string} [hash]
 */
export function isUcAuthHash(hash) {
  const raw = String(hash || "");
  if (!raw) return false;
  const body = raw.charAt(0) === "#" ? raw.slice(1) : raw;
  const path = body.split("?")[0].replace(/\/+$/, "") || "/";
  return path === "/auth" || path === "auth";
}

/**
 * @param {string} pathname
 * @param {string} hash
 * @param {boolean} hasToken
 */
export function ucAuthLandingTarget(pathname, hash, hasToken) {
  if (isUcAuthPath(pathname)) return "";
  const here = String(pathname || "/").split("?")[0].split("#")[0] || "/";
  const atRoot = here === "/" || here === "/index.html";
  if (atRoot && (hasToken || isUcAuthHash(hash))) return "/auth";
  if (hasToken) return "/auth";
  return "";
}

/**
 * @param {{ persist?: boolean }} [opts]
 */
export function consumeWorkbenchToken(opts) {
  const persist = Boolean(opts?.persist);
  const keys = workbenchTokenKeys("token");
  let token = "";
  try {
    const parsed = parseAndStripWorkbenchToken(window.location.href, keys);
    token = parsed.token;
    const cur = window.location.pathname + window.location.search + window.location.hash;
    if (parsed.path !== cur) {
      window.history.replaceState({}, "", parsed.path);
    }
  } catch {
    /* ignore */
  }

  if (token) {
    try {
      if (persist) sessionStorage.setItem(UC_HANDOFF_TOKEN_KEY, token);
      else sessionStorage.removeItem(UC_HANDOFF_TOKEN_KEY);
    } catch {
      /* quota / private mode */
    }
    return token;
  }

  if (persist) return "";

  try {
    const stashed = (sessionStorage.getItem(UC_HANDOFF_TOKEN_KEY) || "").trim();
    if (stashed) sessionStorage.removeItem(UC_HANDOFF_TOKEN_KEY);
    return stashed;
  } catch {
    return "";
  }
}

/** @returns {{ user_id: string, display_name?: string, username?: string, role?: string, tenant_id?: string, session_ticket: string, source?: string } | null} */
export function readUcSession() {
  try {
    const raw = sessionStorage.getItem(UC_SESSION_KEY);
    if (!raw) return null;
    const doc = JSON.parse(raw);
    return doc && doc.user_id && doc.session_ticket ? doc : null;
  } catch {
    return null;
  }
}

/**
 * @param {{ user_id?: string, display_name?: string, username?: string, role?: string, tenant_id?: string, menu_codes?: string[], session_ticket?: string, source?: string }} me
 */
export function persistUcSession(me) {
  const uid = String(me?.user_id || "").trim();
  if (!uid) return;
  try {
    sessionStorage.setItem(
      UC_SESSION_KEY,
      JSON.stringify({
        user_id: uid,
        display_name: me.display_name || "",
        username: me.username || "",
        role: me.role || "user",
        tenant_id: me.tenant_id || "",
        menu_codes: Array.isArray(me.menu_codes) ? me.menu_codes : [],
        session_ticket: me.session_ticket || "",
        source: me.source || "uc",
      }),
    );
  } catch {
    /* quota / private mode */
  }
}

export function clearUcSession() {
  try {
    sessionStorage.removeItem(UC_SESSION_KEY);
  } catch {
    /* ignore */
  }
}

/** @returns {Record<string, string>} */
export function identityHeaders() {
  /** @type {Record<string, string>} */
  const headers = {};
  const sess = readUcSession();
  if (sess?.user_id) headers["X-User-Id"] = sess.user_id;
  if (sess?.session_ticket) headers["X-UC-Session"] = sess.session_ticket;
  return headers;
}

/**
 * @param {string} detail
 */
export function notifyUcExpired(detail) {
  if (!/uc session/i.test(detail)) return false;
  clearUcSession();
  window.dispatchEvent(new Event(UC_EXPIRED_EVENT));
  return true;
}

/**
 * @param {Response} res
 */
export async function noteUcUnauthorized(res) {
  if (res.status !== 401) return false;
  let detail = "";
  try {
    const body = await res.clone().json();
    detail = String(body.detail || body.error || "");
  } catch {
    /* 非 JSON 401 不按 UC 过期处理 */
  }
  return notifyUcExpired(detail);
}

/** @returns {Promise<{ enabled: boolean, mock?: boolean, token_query?: string }>} */
export async function fetchUcStatus() {
  const res = await fetch("/api/auth/uc/status");
  if (!res.ok) throw new Error(`uc status HTTP ${res.status}`);
  return res.json();
}

/**
 * @param {string} token
 */
export async function exchangeUcToken(token) {
  const t = String(token || "").trim();
  if (!t) throw new Error("缺少统一登录 token");
  const res = await fetch("/api/auth/uc/exchange", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: t }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(String(body.detail || body.error || `验票失败（HTTP ${res.status}）`));
  }
  if (!body.user_id || !body.session_ticket) {
    throw new Error("验票响应缺少会话票");
  }
  return body;
}
