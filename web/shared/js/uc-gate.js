/**
 * 开闸后无有效票则全屏拦截，不渲染控制台。
 * @module js/uc-gate
 */

import {
  UC_EXPIRED_EVENT,
  clearUcSession,
  consumeWorkbenchToken,
  exchangeUcToken,
  fetchUcStatus,
  isUcAuthPath,
  persistUcSession,
  readUcSession,
} from "./uc.js";

/**
 * @param {string} title
 * @param {string} body
 */
function showGateScreen(title, body) {
  let el = document.getElementById("ucGate");
  if (!el) {
    el = document.createElement("div");
    el.id = "ucGate";
    el.className = "uc-gate";
    el.setAttribute("role", "alert");
    document.body.appendChild(el);
  }
  el.hidden = false;
  el.innerHTML =
    `<div class="uc-gate-card">` +
    `<h1>${escapeHtml(title)}</h1>` +
    `<p>${escapeHtml(body)}</p>` +
    `</div>`;
  const app = document.querySelector(".app");
  if (app) app.hidden = true;
}

function hideGateScreen() {
  const el = document.getElementById("ucGate");
  if (el) el.hidden = true;
  const app = document.querySelector(".app");
  if (app) app.hidden = false;
}

/**
 * @param {string} s
 */
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function bindExpired() {
  if (window.__AFS_UC_GATE_BOUND__) return;
  window.__AFS_UC_GATE_BOUND__ = true;
  window.addEventListener(UC_EXPIRED_EVENT, () => {
    clearUcSession();
    showGateScreen("需要统一登录", "会话已过期。请从集中统一工作台重新打开本系统。");
  });
}

/**
 * @returns {Promise<boolean>} true = 继续挂载页面
 */
export async function runUcGate() {
  bindExpired();

  if (typeof window !== "undefined" && window.__AFS_UC_AUTH_HOP__) {
    showGateScreen("正在检查登录状态", "请稍候。");
    return false;
  }

  const token = consumeWorkbenchToken({ persist: false });
  let enabled = false;
  try {
    const st = await fetchUcStatus();
    enabled = Boolean(st.enabled);
  } catch {
    /* status 探测失败视为关闸 */
  }

  if (token) {
    showGateScreen("正在验证统一登录", "请稍候，正在向工作台换取会话。");
    try {
      const me = await exchangeUcToken(token);
      persistUcSession(me);
      if (isUcAuthPath(window.location.pathname)) {
        window.location.replace("/");
        return false;
      }
      hideGateScreen();
      return true;
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (enabled) {
        clearUcSession();
        showGateScreen(
          "需要统一登录",
          `请从集中统一工作台打开本系统。直接访问没有认证信息。（${msg}）`,
        );
        return false;
      }
      hideGateScreen();
      return true;
    }
  }

  if (!enabled) {
    hideGateScreen();
    return true;
  }

  const sess = readUcSession();
  if (sess?.session_ticket) {
    if (isUcAuthPath(window.location.pathname)) {
      window.location.replace("/");
      return false;
    }
    hideGateScreen();
    return true;
  }

  showGateScreen(
    "需要统一登录",
    "请从集中统一工作台打开本系统。直接访问没有认证信息。",
  );
  return false;
}
