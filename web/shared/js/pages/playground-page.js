/**
 * API playground page.
 * @module js/pages/playground-page
 */

import { playgroundRequest } from "../api.js";
import { pageHeader, panel } from "../components/page.js";
import { toast } from "../components/toast.js";
import { esc } from "../utils.js";

const PRESETS = [
  {
    id: "list",
    label: "列出文件",
    method: "GET",
    path: "/files",
    query: { limit: 5, offset: 0 },
    body: "",
  },
  {
    id: "health",
    label: "Health",
    method: "GET",
    path: "/health",
    query: {},
    body: "",
    note: "免鉴权（path 以 /health 开头）",
  },
  {
    id: "metrics",
    label: "Metrics 文本",
    method: "GET",
    path: "/metrics",
    query: {},
    body: "",
  },
  {
    id: "status",
    label: "文件 status（填 id）",
    method: "GET",
    path: "/files/{id}/status",
    query: {},
    body: "",
    needsId: true,
  },
  {
    id: "content",
    label: "content?profile=layout",
    method: "GET",
    path: "/files/{id}/content",
    query: { profile: "layout", smart_mode: true },
    body: "",
    needsId: true,
  },
  {
    id: "tables",
    label: "content?profile=tables",
    method: "GET",
    path: "/files/{id}/content",
    query: { profile: "tables" },
    body: "",
    needsId: true,
  },
  {
    id: "parse",
    label: "Trigger layout parse",
    method: "POST",
    path: "/files/{id}/parse",
    query: {},
    body: JSON.stringify({ profile: "layout", force: false }, null, 2),
    needsId: true,
  },
  {
    id: "parse-deep",
    label: "Trigger deep (force)",
    method: "POST",
    path: "/files/{id}/parse",
    query: {},
    body: JSON.stringify({ profile: "deep", force: true }, null, 2),
    needsId: true,
  },
  {
    id: "custom",
    label: "Custom-parse",
    method: "POST",
    path: "/files/{id}/custom-parse",
    query: {},
    body: JSON.stringify({ prompt: "请简述文件内容要点", media_kind: null }, null, 2),
    needsId: true,
  },
  {
    id: "batch-status",
    label: "batch-status",
    method: "POST",
    path: "/files/batch-status",
    query: {},
    body: JSON.stringify({ reference_ids: ["00000000-0000-0000-0000-000000000000"], profile: "layout" }, null, 2),
  },
  {
    id: "archive",
    label: "archive members",
    method: "GET",
    path: "/files/{id}/archive/members",
    query: {},
    body: "",
    needsId: true,
  },
  {
    id: "from-url",
    label: "from-url",
    method: "POST",
    path: "/files/from-url",
    query: {},
    body: JSON.stringify({ url: "https://example.com/file.pdf", auto_parse: true }, null, 2),
  },
];

export function initPlayground() {
  const root = document.getElementById("pageRoot");
  if (!root) return;

  root.innerHTML = `
    ${pageHeader({
      title: "API 调试台",
      sub: "自动注入 Bearer + X-Tenant-Id（config.js）· 生产已关闭 /docs",
    })}
    <div class="playground-layout">
      ${panel({
        title: "预设场景",
        body: `<div class="preset-list" id="presets">
          ${PRESETS.map(
            (p) =>
              `<button type="button" class="secondary" data-preset="${esc(p.id)}">${esc(p.label)}</button>`,
          ).join("")}
        </div>
        <div class="form-field" style="margin-top:0.75rem">
          <label>reference_id（含 {id} 的预设会替换）</label>
          <input id="pgId" class="mono" placeholder="uuid" />
        </div>`,
      })}
      ${panel({
        title: "请求",
        body: `
          <div class="form-grid">
            <div class="form-field">
              <label>method</label>
              <select id="pgMethod">
                <option>GET</option><option>POST</option><option>PATCH</option><option>DELETE</option>
              </select>
            </div>
            <div class="form-field full">
              <label>path（相对 /v1 或绝对 /health）</label>
              <input id="pgPath" class="mono" value="/files?limit=5" />
            </div>
            <div class="form-field full">
              <label>JSON body（GET 忽略）</label>
              <textarea id="pgBody" rows="8" class="mono"></textarea>
            </div>
          </div>
          <div style="margin-top:0.75rem;display:flex;gap:0.5rem">
            <button type="button" id="btnSend">发送</button>
          </div>
        `,
      })}
    </div>
    ${panel({
      title: "响应",
      body: `
        <div class="resp-meta" id="respMeta">尚未发送</div>
        <pre class="md-raw" id="respBody">—</pre>
      `,
    })}
  `;

  document.getElementById("presets")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-preset]");
    if (!btn) return;
    const p = PRESETS.find((x) => x.id === btn.getAttribute("data-preset"));
    if (!p) return;
    applyPreset(p);
  });

  document.getElementById("btnSend")?.addEventListener("click", send);
}

/** @param {typeof PRESETS[0]} p */
function applyPreset(p) {
  let path = p.path;
  if (path.includes("{id}")) {
    const id = val("pgId");
    if (!id) {
      toast("请先填写 reference_id", { type: "err" });
      return;
    }
    path = path.replaceAll("{id}", id);
  }
  const q = p.query || {};
  const qs = Object.entries(q)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
    .join("&");
  if (qs) path += (path.includes("?") ? "&" : "?") + qs;

  setVal("pgMethod", p.method);
  setVal("pgPath", path);
  setVal("pgBody", p.body || "");
  toast(`已载入：${p.label}`, { type: "ok" });
}

async function send() {
  const method = val("pgMethod") || "GET";
  let path = val("pgPath") || "/files";
  if (!path.startsWith("/")) path = `/${path}`;

  // Split query from path for api() helper
  let query = undefined;
  const qi = path.indexOf("?");
  if (qi >= 0) {
    query = Object.fromEntries(new URLSearchParams(path.slice(qi + 1)));
    path = path.slice(0, qi);
  }

  const bodyRaw = val("pgBody");
  let body = null;
  if (method !== "GET" && method !== "HEAD" && bodyRaw) {
    try {
      JSON.parse(bodyRaw);
      body = bodyRaw;
    } catch {
      toast("body 不是合法 JSON", { type: "err" });
      return;
    }
  }

  try {
    const res = await playgroundRequest({ method, path, query, body });
    const meta = document.getElementById("respMeta");
    const box = document.getElementById("respBody");
    if (meta) {
      meta.innerHTML = `
        <span>HTTP <strong>${res.status}</strong> ${res.ok ? "✓" : "✗"}</span>
        <span>${res.elapsedMs} ms</span>
        <span class="mono">${esc(method)} ${esc(path)}</span>
      `;
    }
    if (box) {
      box.textContent =
        typeof res.body === "string" ? res.body : JSON.stringify(res.body, null, 2);
    }
    if (!res.ok) toast(`HTTP ${res.status}`, { type: "err" });
  } catch (err) {
    toast(err.message || String(err), { type: "err" });
    const box = document.getElementById("respBody");
    if (box) box.textContent = err.message || String(err);
  }
}

function val(id) {
  return /** @type {HTMLInputElement|HTMLSelectElement|HTMLTextAreaElement|null} */ (
    document.getElementById(id)
  )?.value?.trim() || "";
}

function setVal(id, v) {
  const el = /** @type {HTMLInputElement|HTMLSelectElement|HTMLTextAreaElement|null} */ (
    document.getElementById(id)
  );
  if (el) el.value = v;
}
