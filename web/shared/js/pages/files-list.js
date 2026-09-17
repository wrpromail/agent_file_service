/**
 * Files list page.
 * @module js/pages/files-list
 */

import { getConfig } from "../../config.js";
import { batchStatus, listFiles } from "../api.js";
import { profileChip } from "../components/badge.js";
import { errorState, loadingState, pageHeader, panel, toolbar } from "../components/page.js";
import { renderPager } from "../components/pager.js";
import { dataTable } from "../components/table.js";
import { toast } from "../components/toast.js";
import { esc, formatBytes, formatDate, qs, truncate } from "../utils.js";

let offset = 0;
let limit = 20;

export async function initFilesList() {
  const root = document.getElementById("pageRoot");
  if (!root) return;
  limit = Number(getConfig().pageSize) || 20;

  const sessionQ = qs("session_id");
  const agentQ = qs("agent_id");

  root.innerHTML = `
    ${pageHeader({
      title: "文件列表",
      sub: "当前 tenant 下的引用句柄 · layout 状态经 batch-status 补齐",
      actionsHtml: `<a class="btn" href="/upload.html">上传</a>
        <button type="button" class="secondary" id="btnReload">刷新</button>`,
    })}
    ${panel({
      body: `
        ${toolbar(`
          <input type="search" id="fFilename" placeholder="文件名包含…" value="" />
          <input type="text" id="fSession" placeholder="session_id" value="${esc(sessionQ)}" />
          <input type="text" id="fAgent" placeholder="agent_id" value="${esc(agentQ)}" />
          <input type="text" id="fHash" placeholder="content_hash" class="mono" />
          <button type="button" class="secondary" id="btnSearch">查询</button>
        `)}
        <div id="tableHost">${loadingState()}</div>
        <div class="pager" id="pagerHost"></div>
      `,
    })}
  `;

  document.getElementById("btnReload")?.addEventListener("click", () => load());
  document.getElementById("btnSearch")?.addEventListener("click", () => {
    offset = 0;
    load();
  });
  ["fFilename", "fSession", "fAgent", "fHash"].forEach((id) => {
    document.getElementById(id)?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        offset = 0;
        load();
      }
    });
  });

  await load();
}

function filters() {
  return {
    filename: val("fFilename"),
    session_id: val("fSession"),
    agent_id: val("fAgent"),
    content_hash: val("fHash"),
    limit,
    offset,
  };
}

function val(id) {
  return /** @type {HTMLInputElement|null} */ (document.getElementById(id))?.value?.trim() || "";
}

async function load() {
  const host = document.getElementById("tableHost");
  const pager = document.getElementById("pagerHost");
  if (!host) return;
  host.innerHTML = loadingState();
  try {
    const data = await listFiles(filters());
    const files = data.files || [];
    /** @type {Record<string, string>} */
    let layoutMap = {};
    if (files.length) {
      try {
        const batch = await batchStatus(
          files.map((f) => f.reference_id),
          "layout",
        );
        for (const item of batch.items || []) {
          if (!item.ok || !item.status) continue;
          const pr = (item.status.parse_results || []).find((p) => p.profile === "layout");
          layoutMap[item.reference_id] = pr?.status || "not_started";
        }
      } catch {
        toast("batch-status 失败，状态徽章暂不可用", { type: "err" });
      }
    }

    host.innerHTML = dataTable({
      empty: "没有匹配的文件",
      columns: [
        {
          key: "name",
          label: "文件名",
          render: (r) =>
            `<a href="/file.html?id=${esc(r.reference_id)}"><strong>${esc(r.original_filename || "—")}</strong></a>
             <div class="muted mono" style="font-size:0.7rem">${esc(truncate(r.reference_id, 13))}</div>`,
        },
        {
          key: "mime",
          label: "类型 / 大小",
          render: (r) =>
            `${esc(r.metadata?.mime_type || "—")}<div class="muted">${esc(formatBytes(r.metadata?.size_bytes))}</div>`,
        },
        {
          key: "session",
          label: "Session",
          render: (r) =>
            r.session_id
              ? `<a href="/session.html?id=${encodeURIComponent(r.session_id)}">${esc(truncate(r.session_id, 18))}</a>`
              : "—",
        },
        {
          key: "layout",
          label: "layout",
          render: (r) => profileChip("layout", layoutMap[r.reference_id] || "—"),
        },
        {
          key: "created",
          label: "创建",
          render: (r) => esc(formatDate(r.created_at)),
        },
        {
          key: "expires",
          label: "过期",
          render: (r) => (r.expires_at ? esc(formatDate(r.expires_at)) : "—"),
        },
      ],
      rows: files,
      rowAttrs: (r) =>
        `class="tr-clickable" data-href="/file.html?id=${esc(r.reference_id)}"`,
    });

    host.querySelectorAll("tr[data-href]").forEach((tr) => {
      tr.addEventListener("click", (e) => {
        if (e.target.closest("a")) return;
        location.href = tr.getAttribute("data-href") || "#";
      });
    });

    if (pager) {
      renderPager(pager, {
        total: data.total || 0,
        limit: data.limit || limit,
        offset: data.offset || offset,
        onChange: (next) => {
          offset = next;
          load();
        },
      });
    }
  } catch (err) {
    host.innerHTML = errorState(err.message || String(err));
    if (pager) pager.innerHTML = "";
  }
}
