/**
 * File detail page (overview / parse / content / custom-parse / raw).
 * @module js/pages/file-detail
 */

import {
  createCustomParse,
  deleteFile,
  downloadFile,
  getArchiveMembers,
  getFile,
  getFileContent,
  getFileStatus,
  listCustomParses,
  triggerParse,
} from "../api.js";
import { getTenantId } from "../auth.js";
import { profileChip, statusChip } from "../components/badge.js";
import {
  emptyState,
  errorState,
  loadingState,
  metaGrid,
  pageHeader,
  panel,
} from "../components/page.js";
import { bindTabs } from "../components/tabs.js";
import { toast } from "../components/toast.js";
import {
  copyText,
  esc,
  formatBytes,
  formatDate,
  qs,
  renderMarkdown,
  truncate,
} from "../utils.js";

const PROFILES = ["layout", "tables", "ocr", "deep", "metadata"];

export async function initFileDetail() {
  const root = document.getElementById("pageRoot");
  if (!root) return;
  const id = qs("id");
  if (!id) {
    root.innerHTML = errorState("缺少 ?id=reference_id");
    return;
  }

  root.innerHTML = `
    ${pageHeader({
      title: "文件详情",
      sub: `<span class="mono">${esc(id)}</span>`,
      actionsHtml: `
        <button type="button" class="secondary" id="btnRefresh">刷新</button>
        <button type="button" class="secondary" id="btnDl">下载原件</button>
        <button type="button" class="danger" id="btnDel">删除</button>
      `,
    })}
    <div id="shell">${loadingState()}</div>
  `;

  document.getElementById("btnRefresh")?.addEventListener("click", () => load(id));
  document.getElementById("btnDl")?.addEventListener("click", async () => {
    try {
      const d = await getFile(id);
      await downloadFile(id, d.metadata?.filename || d.original_filename || "download");
      toast("开始下载", { type: "ok" });
    } catch (e) {
      toast(e.message || String(e), { type: "err" });
    }
  });
  document.getElementById("btnDel")?.addEventListener("click", async () => {
    if (!confirm("确认软删除此引用？")) return;
    try {
      await deleteFile(id);
      toast("已删除", { type: "ok" });
      location.href = "/files.html";
    } catch (e) {
      toast(e.message || String(e), { type: "err" });
    }
  });

  await load(id);
}

/** @param {string} id */
async function load(id) {
  const shell = document.getElementById("shell");
  if (!shell) return;
  shell.innerHTML = loadingState();
  try {
    const [detail, status] = await Promise.all([getFile(id), getFileStatus(id)]);
    const name = detail.metadata?.filename || "file";
    const titleEl = document.querySelector(".page-title");
    if (titleEl) titleEl.textContent = name;

    shell.innerHTML = `
      <div class="tabs" role="tablist">
        <button type="button" class="tab is-active" data-tab="overview">概览</button>
        <button type="button" class="tab" data-tab="parse">解析</button>
        <button type="button" class="tab" data-tab="content">Content</button>
        <button type="button" class="tab" data-tab="custom">Custom-parse</button>
        <button type="button" class="tab" data-tab="raw">Raw</button>
      </div>
      <div data-tab-panel="overview" id="tabOverview"></div>
      <div data-tab-panel="parse" id="tabParse" hidden></div>
      <div data-tab-panel="content" id="tabContent" hidden></div>
      <div data-tab-panel="custom" id="tabCustom" hidden></div>
      <div data-tab-panel="raw" id="tabRaw" hidden></div>
    `;

    renderOverview(detail, status);
    renderParse(id, status);
    renderContentShell(id);
    renderCustomShell(id);
    renderRaw(id, detail, status);

    const tab = qs("tab") || "overview";
    bindTabs(shell, {
      initial: tab,
      onChange: (t) => {
        const u = new URL(location.href);
        u.searchParams.set("tab", t);
        history.replaceState(null, "", u.toString());
        if (t === "content") loadContent(id);
        if (t === "custom") loadCustom(id);
      },
    });
    if (tab === "content") loadContent(id);
    if (tab === "custom") loadCustom(id);
  } catch (err) {
    shell.innerHTML = errorState(err.message || String(err));
  }
}

/** @param {any} detail @param {any} status */
function renderOverview(detail, status) {
  const el = document.getElementById("tabOverview");
  if (!el) return;
  const md = detail.metadata || {};
  const results = status.parse_results || detail.parse_results || [];
  el.innerHTML = panel({
    title: "引用与元数据",
    body: `
      ${metaGrid([
        ["reference_id", `<span class="mono">${esc(detail.reference_id)}</span>`],
        ["content_hash", `<span class="mono">${esc(detail.content_hash)}</span>`],
        ["tenant_id", esc(detail.tenant_id)],
        [
          "session_id",
          detail.session_id
            ? `<a href="/session.html?id=${encodeURIComponent(detail.session_id)}">${esc(detail.session_id)}</a>`
            : "—",
        ],
        ["agent_id", esc(detail.agent_id || "—")],
        ["expires_at", esc(formatDate(detail.expires_at))],
        ["filename", esc(md.filename || "—")],
        ["mime_type", esc(md.mime_type || "—")],
        ["size", esc(formatBytes(md.size_bytes))],
        ["page_count", esc(md.page_count != null ? String(md.page_count) : "—")],
        ["sheet_count", esc(md.sheet_count != null ? String(md.sheet_count) : "—")],
      ])}
      <h3 style="margin:1rem 0 0.5rem;font-size:0.9rem">parse_results</h3>
      <div class="chips">
        ${
          results.length
            ? results.map((p) => profileChip(p.profile, p.status)).join("")
            : '<span class="muted">无</span>'
        }
      </div>
      <h3 style="margin:1rem 0 0.5rem;font-size:0.9rem">custom_metadata</h3>
      <pre class="md-raw">${esc(JSON.stringify(detail.custom_metadata || {}, null, 2))}</pre>
      <h3 style="margin:1rem 0 0.5rem;font-size:0.9rem">metadata.extra</h3>
      <pre class="md-raw">${esc(JSON.stringify(md.extra || {}, null, 2))}</pre>
      <div id="archiveBox"></div>
    `,
  });
  loadArchive(detail.reference_id);
}

/** @param {string} id */
async function loadArchive(id) {
  const box = document.getElementById("archiveBox");
  if (!box) return;
  try {
    const members = await getArchiveMembers(id);
    if (!members) return;
    const list = members.members || [];
    box.innerHTML = `
      <h3 style="margin:1rem 0 0.5rem;font-size:0.9rem">压缩包成员 ${statusChip(members.status || "—")}
        <span class="muted">${esc(members.format || "")}</span>
      </h3>
      ${
        members.message
          ? `<div class="muted" style="margin-bottom:0.35rem">${esc(members.message)}</div>`
          : ""
      }
      ${
        list.length
          ? `<ul style="margin-left:1rem">${list
              .map((m) => {
                const path = m.path || m.filename || "—";
                const size = m.size != null ? ` · ${m.size} B` : "";
                const skip = m.skip_reason ? ` (${m.skip_reason})` : "";
                return `<li><span class="mono">${esc(path)}</span>${esc(size)}${esc(skip)}</li>`;
              })
              .join("")}</ul>`
          : `<div class="muted">无成员列表</div>`
      }
    `;
  } catch {
    /* not an archive or unsupported — ignore */
  }
}

/** @param {string} id @param {any} status */
function renderParse(id, status) {
  const el = document.getElementById("tabParse");
  if (!el) return;
  const map = Object.fromEntries((status.parse_results || []).map((p) => [p.profile, p]));

  el.innerHTML = panel({
    title: "解析管线",
    actionsHtml: `<button type="button" class="secondary" id="btnReloadParse">刷新状态</button>`,
    body: `
      <div class="pipeline-list" id="pipeList">
        ${PROFILES.map((p) => {
          const row = map[p];
          const st = row?.status || "not_started";
          return `<div class="pipeline-row" data-profile="${esc(p)}">
            <span class="pipe-name">${esc(p)}</span>
            ${statusChip(st)}
            <span class="muted mono">${esc(row?.parser_name || "")}</span>
            <span style="flex:1"></span>
            <button type="button" class="secondary" data-act="trigger" data-profile="${esc(p)}">Trigger</button>
            <button type="button" class="ghost" data-act="force" data-profile="${esc(p)}">Force</button>
          </div>`;
        }).join("")}
      </div>
      <p class="muted" style="margin-top:0.75rem">layout / tables 常为默认保证线；ocr / deep 为能力菜单，按需 trigger。</p>
    `,
  });

  el.querySelector("#btnReloadParse")?.addEventListener("click", async () => {
    try {
      const st = await getFileStatus(id);
      renderParse(id, st);
      toast("已刷新", { type: "ok" });
    } catch (e) {
      toast(e.message || String(e), { type: "err" });
    }
  });

  el.querySelectorAll("[data-act]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const profile = btn.getAttribute("data-profile") || "layout";
      const force = btn.getAttribute("data-act") === "force";
      try {
        const res = await triggerParse(id, { profile, force });
        toast(`${profile} → ${res.status}`, { type: "ok" });
        const st = await getFileStatus(id);
        renderParse(id, st);
      } catch (e) {
        toast(e.message || String(e), { type: "err" });
      }
    });
  });
}

/** @param {string} id */
function renderContentShell(id) {
  const el = document.getElementById("tabContent");
  if (!el) return;
  el.innerHTML = panel({
    title: "Content 预览",
    body: `
      <div class="content-toolbar">
        <label>profile
          <select id="cProfile">
            <option value="layout" selected>layout</option>
            <option value="tables">tables</option>
            <option value="ocr">ocr</option>
            <option value="deep">deep</option>
          </select>
        </label>
        <label>smart_mode
          <select id="cSmart"><option value="true" selected>true</option><option value="false">false</option></select>
        </label>
        <label>mode
          <select id="cMode"><option value="full" selected>full</option><option value="outline">outline</option></select>
        </label>
        <label>max_chars <input id="cMax" type="number" min="0" placeholder="—" style="width:6rem" /></label>
        <label>sheets <input id="cSheets" placeholder="Sheet1,Sheet2" style="width:8rem" /></label>
        <button type="button" id="btnLoadContent">加载</button>
        <button type="button" class="secondary" id="btnToggleRaw">原文 / 渲染</button>
      </div>
      <div id="contentBody">${loadingState("切换到此 Tab 或点击加载")}</div>
    `,
  });
  el.querySelector("#btnLoadContent")?.addEventListener("click", () => loadContent(id));
  el.querySelector("#btnToggleRaw")?.addEventListener("click", () => {
    const pre = document.getElementById("mdRawView");
    const md = document.getElementById("mdHtmlView");
    if (!pre || !md) return;
    const showRaw = pre.hidden;
    pre.hidden = !showRaw;
    md.hidden = showRaw;
  });
}

/** @param {string} id */
async function loadContent(id) {
  const body = document.getElementById("contentBody");
  if (!body) return;
  body.innerHTML = loadingState();
  const profile = val("cProfile") || "layout";
  const query = {
    profile,
    smart_mode: val("cSmart") !== "false",
    mode: val("cMode") || "full",
  };
  const max = val("cMax");
  if (max) query.max_chars = Number(max);
  const sheets = val("cSheets");
  if (sheets) query.sheets = sheets;

  try {
    const c = await getFileContent(id, query);
    if (!c.ready) {
      body.innerHTML = `
        <div style="margin-bottom:0.5rem">${statusChip(c.status || "not_ready")}
          <span class="chip info">${esc(c.message_code || "")}</span>
        </div>
        <p>${esc(c.message || "内容未就绪")}</p>
        <pre class="md-raw">${esc(JSON.stringify(c, null, 2))}</pre>
      `;
      return;
    }

    if (profile === "tables") {
      const tables = c.metadata?.tables_json || c.metadata?.tables || c;
      body.innerHTML = `
        <div style="margin-bottom:0.5rem">${statusChip("completed")}</div>
        <pre class="md-raw">${esc(JSON.stringify(tables, null, 2))}</pre>
      `;
      return;
    }

    const md =
      c.metadata?.markdown ||
      (c.blocks || []).map((b) => b.text || "").filter(Boolean).join("\n\n") ||
      "";
    body.innerHTML = `
      <div style="margin-bottom:0.5rem">${statusChip("completed")}
        <span class="muted">chars≈${md.length}</span>
      </div>
      <div id="mdHtmlView">${renderMarkdown(md)}</div>
      <pre class="md-raw" id="mdRawView" hidden>${esc(md)}</pre>
    `;
  } catch (err) {
    body.innerHTML = errorState(err.message || String(err));
  }
}

/** @param {string} id */
function renderCustomShell(id) {
  const el = document.getElementById("tabCustom");
  if (!el) return;
  el.innerHTML = panel({
    title: "Custom-parse",
    body: `
      <div class="form-grid">
        <div class="form-field full"><label>prompt</label>
          <textarea id="cpPrompt" rows="3" placeholder="请识别图中关键信息…"></textarea>
        </div>
        <div class="form-field">
          <label>media_kind（可空自动推断）</label>
          <select id="cpKind">
            <option value="">auto</option>
            <option value="image">image</option>
            <option value="audio">audio</option>
            <option value="video">video</option>
          </select>
        </div>
        <div class="form-field"><label>created_by</label><input id="cpBy" placeholder="dev" /></div>
      </div>
      <div style="margin:0.75rem 0;display:flex;gap:0.5rem">
        <button type="button" id="btnCpCreate">创建任务</button>
        <button type="button" class="secondary" id="btnCpReload">刷新列表</button>
      </div>
      <div id="cpList">${loadingState()}</div>
    `,
  });
  el.querySelector("#btnCpCreate")?.addEventListener("click", async () => {
    const prompt = val("cpPrompt");
    if (!prompt) {
      toast("请填写 prompt", { type: "err" });
      return;
    }
    try {
      const job = await createCustomParse(id, {
        prompt,
        media_kind: val("cpKind") || null,
        created_by: val("cpBy") || null,
      });
      toast(`job ${job.status}`, { type: "ok" });
      await loadCustom(id);
      pollJob(id, job.job_id);
    } catch (e) {
      toast(e.message || String(e), { type: "err" });
    }
  });
  el.querySelector("#btnCpReload")?.addEventListener("click", () => loadCustom(id));
}

/** @param {string} id */
async function loadCustom(id) {
  const list = document.getElementById("cpList");
  if (!list) return;
  list.innerHTML = loadingState();
  try {
    const data = await listCustomParses(id, { limit: 50, offset: 0 });
    const jobs = data.jobs || [];
    if (!jobs.length) {
      list.innerHTML = emptyState("暂无 custom-parse 任务");
      return;
    }
    list.innerHTML = jobs
      .map(
        (j) => `
      <div class="pipeline-row" style="flex-direction:column;align-items:stretch">
        <div style="display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap">
          <span class="mono">${esc(truncate(j.job_id, 13))}</span>
          ${statusChip(j.status)}
          <span class="chip info">${esc(j.media_kind)}</span>
          <span class="muted">${esc(formatDate(j.created_at))}</span>
        </div>
        <div style="margin-top:0.35rem">${esc(truncate(j.prompt, 200))}</div>
        ${j.error ? `<div class="muted" style="color:var(--color-danger)">${esc(j.error)}</div>` : ""}
        ${
          j.result_text
            ? `<pre class="md-raw" style="margin-top:0.5rem;max-height:240px">${esc(j.result_text)}</pre>`
            : ""
        }
      </div>`,
      )
      .join("");
  } catch (e) {
    list.innerHTML = errorState(e.message || String(e));
  }
}

/** @param {string} id @param {string} jobId */
async function pollJob(id, jobId) {
  for (let i = 0; i < 30; i++) {
    await sleep(1000);
    try {
      const { getCustomParse } = await import("../api.js");
      const j = await getCustomParse(id, jobId);
      if (j.status === "completed" || j.status === "failed") {
        await loadCustom(id);
        toast(`job ${j.status}`, { type: j.status === "completed" ? "ok" : "err" });
        return;
      }
    } catch {
      return;
    }
  }
}

/** @param {string} id @param {any} detail @param {any} status */
function renderRaw(id, detail, status) {
  const el = document.getElementById("tabRaw");
  if (!el) return;
  // Never embed the real Bearer in DOM / clipboard (XSS, shoulder-surfing, screenshots).
  const tenant = getTenantId() || "<TENANT>";
  const curl = `curl -sS "${location.origin}/v1/files/${id}" \\\n  -H "Authorization: Bearer <API_KEY>" \\\n  -H "X-Tenant-Id: ${tenant}"`;

  el.innerHTML = panel({
    title: "Raw / curl",
    actionsHtml: `<button type="button" class="secondary" id="btnCopyCurl">复制 curl</button>`,
    body: `
      <h3 style="font-size:0.85rem;margin-bottom:0.35rem">GET detail</h3>
      <pre class="md-raw" id="rawDetail">${esc(JSON.stringify(detail, null, 2))}</pre>
      <h3 style="font-size:0.85rem;margin:0.75rem 0 0.35rem">GET status</h3>
      <pre class="md-raw">${esc(JSON.stringify(status, null, 2))}</pre>
      <h3 style="font-size:0.85rem;margin:0.75rem 0 0.35rem">curl</h3>
      <p class="muted" style="font-size:0.8rem;margin:0 0 0.35rem">Bearer 使用占位符 &lt;API_KEY&gt;；请从部署配置 / 环境变量填入，勿粘贴真实密钥到聊天或截图。</p>
      <pre class="md-raw" id="curlBox">${esc(curl)}</pre>
    `,
  });
  el.querySelector("#btnCopyCurl")?.addEventListener("click", async () => {
    const ok = await copyText(curl);
    toast(ok ? "已复制（含 <API_KEY> 占位符）" : "复制失败", { type: ok ? "ok" : "err" });
  });
}

function val(id) {
  return /** @type {HTMLInputElement|HTMLSelectElement|HTMLTextAreaElement|null} */ (
    document.getElementById(id)
  )?.value?.trim() || "";
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}
