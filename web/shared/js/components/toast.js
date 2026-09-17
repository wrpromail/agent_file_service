/**
 * Lightweight toast notifications.
 * @module js/components/toast
 */

let host = null;

function ensureHost() {
  if (host && document.body.contains(host)) return host;
  host = document.createElement("div");
  host.className = "toast-host";
  host.setAttribute("aria-live", "polite");
  document.body.appendChild(host);
  return host;
}

/**
 * @param {string} message
 * @param {{ type?: "ok"|"err"|"info", ms?: number }} [opts]
 */
export function toast(message, opts = {}) {
  const type = opts.type || "info";
  const ms = opts.ms ?? 3200;
  const el = document.createElement("div");
  el.className = `toast${type === "ok" ? " is-ok" : type === "err" ? " is-err" : ""}`;
  el.textContent = String(message || "");
  ensureHost().appendChild(el);
  requestAnimationFrame(() => el.classList.add("is-in"));
  setTimeout(() => {
    el.classList.remove("is-in");
    setTimeout(() => el.remove(), 200);
  }, ms);
}
