/**
 * Session files page.
 * @module js/pages/session-page
 */

import { batchStatus, listSessionFiles } from "../api.js";
import { profileChip } from "../components/badge.js";
import { errorState, loadingState, pageHeader, panel, toolbar } from "../components/page.js";
import { dataTable } from "../components/table.js";
import { toast } from "../components/toast.js";
import { esc, formatBytes, formatDate, qs, truncate } from "../utils.js";

export function initSession() {
  const root = document.getElementById("pageRoot");
  if (!root) return;
  const initial = qs("id") || qs("session_id") || "";

  root.innerHTML = `
    ${pageHeader({
      title: "会话附件",
      sub: "GET /v1/sessions/{session_id}/files",
    })}
    ${panel({
      body: `
        ${toolbar(`
          <input type="text" id="sessId" placeholder="session_id" value="${esc(initial)}" style="min-width:16rem" />
          <button type="button" id="btnGo">查询</button>
        `)}
        <div id="sessBody">${initial ? loadingState() : `<div class="muted">输入 session_id 后查询</div>`}</div>
      `,
    })}
  `;

  document.getElementById("btnGo")?.addEventListener("click", () => {
    const id = val("sessId");
    if (!id) {
      toast("请输入 session_id", { type: "err" });
      return;
    }
    const u = new URL(location.href);
    u.searchParams.set("id", id);
    history.replaceState(null, "", u.toString());
    load(id);
  });
  document.getElementById("sessId")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("btnGo")?.click();
  });

  if (initial) load(initial);
}

/** @param {string} sessionId */
async function load(sessionId) {
  const body = document.getElementById("sessBody");
  if (!body) return;
  body.innerHTML = loadingState();
  try {
    const data = await listSessionFiles(sessionId, { limit: 100, offset: 0 });
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
        /* ignore */
      }
    }

    body.innerHTML = `
      <p class="muted" style="margin-bottom:0.5rem">session <strong class="mono">${esc(sessionId)}</strong> · ${files.length} 个文件</p>
      ${dataTable({
        empty: "该会话下无文件",
        columns: [
          {
            key: "name",
            label: "文件名",
            render: (r) =>
              `<a href="/file.html?id=${esc(r.reference_id)}">${esc(r.original_filename || r.metadata?.filename || "—")}</a>
               <div class="muted mono" style="font-size:0.7rem">${esc(truncate(r.reference_id, 13))}</div>`,
          },
          {
            key: "size",
            label: "大小",
            render: (r) => esc(formatBytes(r.metadata?.size_bytes)),
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
        ],
        rows: files,
      })}
    `;
  } catch (err) {
    body.innerHTML = errorState(err.message || String(err));
  }
}

function val(id) {
  return /** @type {HTMLInputElement|null} */ (document.getElementById(id))?.value?.trim() || "";
}
