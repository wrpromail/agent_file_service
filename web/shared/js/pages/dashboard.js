/**
 * Dashboard page.
 * @module js/pages/dashboard
 */

import { getHealth, getMetricsText, listFiles } from "../api.js";
import { emptyState, errorState, loadingState, pageHeader, panel } from "../components/page.js";
import { statusChip } from "../components/badge.js";
import { dataTable } from "../components/table.js";
import {
  esc,
  formatBytes,
  formatDate,
  parsePrometheusText,
  sumMetric,
  truncate,
} from "../utils.js";

export async function initDashboard() {
  const root = document.getElementById("pageRoot");
  if (!root) return;

  root.innerHTML = `
    ${pageHeader({
      title: "开发大盘",
      sub: "依赖健康 · Prometheus 计数 · 最近文件（当前 tenant）",
      actionsHtml: `<button type="button" class="secondary" id="btnRefresh">刷新</button>
        <a class="btn ghost" href="/upload.html">上传</a>`,
    })}
    <div class="kpi-grid" id="kpiGrid">
      <a class="kpi-card" href="/metrics.html">
        <div class="kpi-label">上传次数</div>
        <div class="kpi-value" id="kpiUploads">—</div>
        <div class="kpi-hint">uploads_total</div>
      </a>
      <div class="kpi-card">
        <div class="kpi-label">去重命中</div>
        <div class="kpi-value" id="kpiDedup">—</div>
        <div class="kpi-hint">dedup_hits_total</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">解析完成</div>
        <div class="kpi-value" id="kpiParseOk">—</div>
        <div class="kpi-hint">parse_completed_total</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">解析失败</div>
        <div class="kpi-value" id="kpiParseFail">—</div>
        <div class="kpi-hint">parse_failed_total</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">配额拒绝</div>
        <div class="kpi-value" id="kpiQuota">—</div>
        <div class="kpi-hint">quota_rejects_total</div>
      </div>
    </div>
    <div class="dash-grid">
      ${panel({
        title: "依赖健康",
        actionsHtml: `<button type="button" class="ghost" id="btnDeep">deep check</button>`,
        bodyId: "depPanel",
        body: loadingState(),
      })}
      ${panel({
        title: "最近文件",
        actionsHtml: `<a class="btn ghost" href="/files.html">全部</a>`,
        bodyId: "filesPanel",
        tight: true,
        body: loadingState(),
      })}
    </div>
  `;

  document.getElementById("btnRefresh")?.addEventListener("click", () => load());
  document.getElementById("btnDeep")?.addEventListener("click", () => loadDeps(true));
  await load();
}

async function load() {
  await Promise.all([loadMetrics(), loadDeps(false), loadFiles()]);
}

async function loadMetrics() {
  try {
    const text = await getMetricsText();
    const rows = parsePrometheusText(text);
    setText("kpiUploads", String(sumMetric(rows, "uploads_total")));
    setText("kpiDedup", String(sumMetric(rows, "dedup_hits_total")));
    setText("kpiParseOk", String(sumMetric(rows, "parse_completed_total")));
    setText("kpiParseFail", String(sumMetric(rows, "parse_failed_total")));
    setText("kpiQuota", String(sumMetric(rows, "quota_rejects_total")));
  } catch {
    ["kpiUploads", "kpiDedup", "kpiParseOk", "kpiParseFail", "kpiQuota"].forEach((id) =>
      setText(id, "?"),
    );
  }
}

async function loadDeps(deep) {
  const el = document.getElementById("depPanel");
  if (!el) return;
  el.innerHTML = loadingState(deep ? "深度探测中…" : "加载中…");
  try {
    const h = await getHealth(deep);
    const checks = h.dependencies?.checks || [];
    const status = h.status || "unknown";
    if (!checks.length) {
      el.innerHTML = `
        <div style="margin-bottom:0.5rem">${statusChip(status)}</div>
        ${emptyState("无依赖检查结果（可能未启用 startup_dependency_check）")}
      `;
      return;
    }
    el.innerHTML = `
      <div style="margin-bottom:0.65rem">整体 ${statusChip(status)}</div>
      <div class="dep-list">
        ${checks
          .map((c) => {
            const ok = c.ok || c.skipped;
            return `<div class="dep-item">
              <div>
                <div class="dep-name">${esc(c.name)}</div>
                <div class="dep-detail">${esc(c.detail || (c.skipped ? "skipped" : ""))}</div>
              </div>
              ${statusChip(c.skipped ? "skipped" : ok ? "ok" : "failed")}
            </div>`;
          })
          .join("")}
      </div>
    `;
  } catch (err) {
    el.innerHTML = errorState(err.message || String(err));
  }
}

async function loadFiles() {
  const el = document.getElementById("filesPanel");
  if (!el) return;
  el.innerHTML = loadingState();
  try {
    const data = await listFiles({ limit: 10, offset: 0 });
    const files = data.files || [];
    el.innerHTML = dataTable({
      empty: "暂无文件 — 去「上传」造几条数据",
      columns: [
        {
          key: "original_filename",
          label: "文件名",
          render: (r) =>
            `<a href="/file.html?id=${esc(r.reference_id)}">${esc(truncate(r.original_filename || r.reference_id, 40))}</a>`,
        },
        {
          key: "size",
          label: "大小",
          render: (r) => esc(formatBytes(r.metadata?.size_bytes)),
        },
        {
          key: "session_id",
          label: "Session",
          render: (r) =>
            r.session_id
              ? `<a class="mono" href="/session.html?id=${encodeURIComponent(r.session_id)}">${esc(truncate(r.session_id, 16))}</a>`
              : "—",
        },
        {
          key: "created_at",
          label: "创建",
          render: (r) => esc(formatDate(r.created_at)),
        },
      ],
      rows: files,
      rowAttrs: (r) =>
        `class="tr-clickable" data-href="/file.html?id=${esc(r.reference_id)}"`,
    });
    el.querySelectorAll("tr[data-href]").forEach((tr) => {
      tr.addEventListener("click", (e) => {
        if (e.target.closest("a")) return;
        location.href = tr.getAttribute("data-href") || "#";
      });
    });
  } catch (err) {
    el.innerHTML = errorState(err.message || String(err));
  }
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}
