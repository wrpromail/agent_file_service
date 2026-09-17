/**
 * Product chrome: header nav, health badge, tenant readout.
 * @module js/chrome
 */

import { getConfig } from "../config.js";
import { api, getHealth } from "./api.js";
import { getTenantId } from "./auth.js";
import { readUcSession } from "./uc.js";
import { esc } from "./utils.js";

/** @type {const} */
const NAV = [
  { id: "dashboard", href: "/dashboard.html", label: "大盘" },
  { id: "files", href: "/files.html", label: "文件" },
  { id: "upload", href: "/upload.html", label: "上传" },
  { id: "session", href: "/session.html", label: "会话" },
  { id: "metrics", href: "/metrics.html", label: "指标" },
  { id: "playground", href: "/playground.html", label: "调试" },
];

/**
 * @param {string} active
 */
export function mountChrome(active) {
  const cfg = getConfig();
  const brandName = cfg.productName || "Agent File Service";
  const app = document.querySelector(".app") || document.body;

  let header = document.querySelector("header.header");
  if (!header) {
    header = document.createElement("header");
    header.className = "header";
    app.insertBefore(header, app.firstChild);
  }

  const navHtml = NAV.map((item) => {
    const on =
      active === item.id ||
      (item.id === "files" && active === "file");
    return `<a class="nav-link${on ? " is-active" : ""}" href="${item.href}"${
      on ? ' aria-current="page"' : ""
    }>${esc(item.label)}</a>`;
  }).join("\n        ");

  const tenant = getTenantId() || "—";
  const sess = readUcSession();
  const who = (sess && (sess.display_name || sess.user_id)) || "";

  header.innerHTML = `
    <div class="brand">
      <span class="brand-dot" id="connDot" title="服务连接状态"></span>
      <span class="brand-name">${esc(brandName)}</span>
      <nav class="main-nav" aria-label="主导航">${navHtml}</nav>
    </div>
    <div class="header-actions">
      ${
        who
          ? `<span class="badge mono" id="ucUserBadge" title="统一登录工号">${esc(who)}</span>`
          : ""
      }
      <span class="badge mono" id="tenantBadge" title="X-Tenant-Id（config.js 部署写入，页面不可改）">${esc(tenant)}</span>
      <span class="badge" id="healthBadge" title="服务状态">…</span>
    </div>
  `;

  ensureFooter();
  refreshHealth();
}

function ensureFooter() {
  if (document.getElementById("versionFooter")) return;
  const foot = document.createElement("div");
  foot.className = "version-footer";
  foot.id = "versionFooter";
  foot.textContent = "开发控制台 · 非业务网盘";
  document.body.appendChild(foot);
}

export async function refreshHealth() {
  const dot = document.getElementById("connDot");
  const badge = document.getElementById("healthBadge");
  try {
    const live = await getHealth(false);
    const status = live.status || "unknown";
    try {
      await api("/files", { query: { limit: 1, offset: 0 } });
      if (dot) {
        dot.className = "brand-dot is-ok";
        dot.title = status === "ok" ? "已连接" : `存活 · ${status}`;
      }
      if (badge) {
        badge.textContent =
          status === "ok" ? "healthy" : `degraded`;
        badge.title = status === "ok" ? "服务正常" : "依赖 degraded，见大盘";
      }
    } catch (err) {
      // @ts-expect-error status
      const st = err?.status;
      if (dot) {
        dot.className = "brand-dot is-warn";
        dot.title = "存活 · 鉴权/租户失败";
      }
      if (badge) {
        badge.textContent = st === 403 ? "租户拒绝" : "鉴权失败";
      }
    }
  } catch {
    if (dot) {
      dot.className = "brand-dot is-err";
      dot.title = "无法连接";
    }
    if (badge) badge.textContent = "offline";
  }
}
