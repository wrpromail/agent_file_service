/**
 * Metrics visualization page.
 * @module js/pages/metrics-page
 */

import { getHealth, getMetricsText } from "../api.js";
import { statusChip } from "../components/badge.js";
import { errorState, loadingState, pageHeader, panel } from "../components/page.js";
import { dataTable } from "../components/table.js";
import { esc, parsePrometheusText, sumMetric } from "../utils.js";

const GROUPS = [
  {
    title: "上传 / 存储",
    names: ["uploads_total", "upload_bytes_total", "dedup_hits_total", "downloads_total", "deletes_total", "from_url_total", "archive_expand_total"],
  },
  {
    title: "解析",
    names: [
      "parse_started_total",
      "parse_completed_total",
      "parse_failed_total",
      "content_reads_total",
      "custom_parse_started_total",
      "custom_parse_completed_total",
      "custom_parse_failed_total",
    ],
  },
  {
    title: "配额 / HTTP",
    names: ["quota_rejects_total", "http_requests_total"],
  },
];

export async function initMetrics() {
  const root = document.getElementById("pageRoot");
  if (!root) return;

  root.innerHTML = `
    ${pageHeader({
      title: "Prometheus 指标",
      sub: "解析 GET /metrics 文本 · 进程内 counter（免鉴权，仅内网）",
      actionsHtml: `
        <button type="button" id="btnRefresh">刷新</button>
        <button type="button" class="secondary" id="btnHealth">deep health</button>
        <a class="btn ghost" href="/metrics" target="_blank">原始 /metrics</a>
      `,
    })}
    <div class="kpi-grid" id="kpiGrid"></div>
    <div id="groups">${loadingState()}</div>
    ${panel({ title: "全部样本", bodyId: "allRows", tight: true, body: loadingState() })}
    ${panel({ title: "Health deep", bodyId: "healthBox", body: `<div class="muted">点击 deep health 探测</div>` })}
  `;

  document.getElementById("btnRefresh")?.addEventListener("click", () => load());
  document.getElementById("btnHealth")?.addEventListener("click", () => loadHealth());
  await load();
}

async function load() {
  const groups = document.getElementById("groups");
  const all = document.getElementById("allRows");
  const kpi = document.getElementById("kpiGrid");
  try {
    const text = await getMetricsText();
    const rows = parsePrometheusText(text);

    if (kpi) {
      kpi.innerHTML = [
        ["uploads_total", "上传"],
        ["parse_completed_total", "解析完成"],
        ["parse_failed_total", "解析失败"],
        ["content_reads_total", "读 content"],
        ["http_requests_total", "HTTP"],
      ]
        .map(
          ([name, label]) => `
        <div class="kpi-card">
          <div class="kpi-label">${esc(label)}</div>
          <div class="kpi-value">${esc(String(sumMetric(rows, name)))}</div>
          <div class="kpi-hint mono">${esc(name)}</div>
        </div>`,
        )
        .join("");
    }

    if (groups) {
      groups.innerHTML = GROUPS.map((g) => {
        const subset = rows.filter((r) => g.names.includes(r.name));
        return panel({
          title: g.title,
          tight: true,
          body: dataTable({
            empty: "本组暂无样本（计数器惰性创建，未触发则为空）",
            columns: [
              { key: "name", label: "metric", render: (r) => `<span class="mono">${esc(r.name)}</span>` },
              {
                key: "labels",
                label: "labels",
                render: (r) => {
                  const keys = Object.keys(r.labels || {});
                  if (!keys.length) return "—";
                  return esc(keys.map((k) => `${k}=${r.labels[k]}`).join(", "));
                },
              },
              { key: "value", label: "value", render: (r) => esc(String(r.value)) },
            ],
            rows: subset.length
              ? subset
              : g.names.map((name) => ({ name, labels: {}, value: sumMetric(rows, name) })),
          }),
        });
      }).join("");
    }

    if (all) {
      all.innerHTML = dataTable({
        empty: "无指标（服务刚启动且无流量）",
        columns: [
          { key: "name", label: "metric", render: (r) => `<span class="mono">${esc(r.name)}</span>` },
          {
            key: "labels",
            label: "labels",
            render: (r) => {
              const keys = Object.keys(r.labels || {});
              return keys.length ? esc(keys.map((k) => `${k}="${r.labels[k]}"`).join(", ")) : "—";
            },
          },
          { key: "value", label: "value", render: (r) => esc(String(r.value)) },
        ],
        rows,
      });
    }
  } catch (err) {
    if (groups) groups.innerHTML = errorState(err.message || String(err));
    if (all) all.innerHTML = errorState(err.message || String(err));
  }
}

async function loadHealth() {
  const box = document.getElementById("healthBox");
  if (!box) return;
  box.innerHTML = loadingState("deep check…");
  try {
    const h = await getHealth(true);
    box.innerHTML = `
      <div style="margin-bottom:0.5rem">status ${statusChip(h.status || "—")}</div>
      <pre class="md-raw">${esc(JSON.stringify(h, null, 2))}</pre>
    `;
  } catch (err) {
    box.innerHTML = errorState(err.message || String(err));
  }
}
