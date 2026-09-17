/**
 * Shared DOM / string helpers.
 * @module js/utils
 */

/** @param {unknown} s */
export function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** @param {string} name */
export function qs(name, url = window.location.href) {
  try {
    return new URL(url).searchParams.get(name) || "";
  } catch {
    return "";
  }
}

/** @param {Record<string, string|number|boolean|null|undefined>} params */
export function buildQuery(params) {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v === undefined || v === null || v === "") continue;
    sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

/** @param {string|null|undefined} iso */
export function formatDate(iso) {
  if (!iso) return "—";
  const s = String(iso).replace("T", " ").replace("Z", " UTC");
  return s.length > 23 ? s.slice(0, 23) : s;
}

/** @param {string} s @param {number} n */
export function truncate(s, n = 80) {
  const t = String(s || "");
  if (t.length <= n) return t;
  return `${t.slice(0, n - 1)}…`;
}

/** @param {number} n */
export function formatBytes(n) {
  const v = Number(n) || 0;
  if (v < 1024) return `${v} B`;
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`;
  if (v < 1024 * 1024 * 1024) return `${(v / (1024 * 1024)).toFixed(1)} MB`;
  return `${(v / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

/**
 * Parse status → chip class (completed/pending/failed/…).
 * @param {string} status
 */
export function parseStatusChipClass(status) {
  const s = String(status || "").toLowerCase();
  if (s === "completed" || s === "ok" || s === "expanded") return "ok";
  if (s === "pending" || s === "processing" || s === "not_started") return "warn";
  if (s === "failed" || s === "error" || s === "password_required") return "danger";
  if (s === "active") return "active";
  return "info";
}

/**
 * Zero-dependency Markdown → safe HTML (subset for layout preview).
 * @param {string} md
 */
export function renderMarkdown(md) {
  const src = String(md || "");
  if (!src.trim()) return `<p class="muted">（空）</p>`;

  // Extract fenced code blocks first
  /** @type {string[]} */
  const fences = [];
  let text = src.replace(/```([\w-]*)\n?([\s\S]*?)```/g, (_m, lang, code) => {
    const i = fences.length;
    fences.push(
      `<pre class="md-code"><code class="lang-${esc(lang || "text")}">${esc(code.replace(/\n$/, ""))}</code></pre>`,
    );
    return `\n%%FENCE${i}%%\n`;
  });

  const lines = text.split("\n");
  /** @type {string[]} */
  const out = [];
  let inUl = false;
  let inOl = false;
  let inTable = false;
  /** @type {string[]} */
  let tableRows = [];

  const closeLists = () => {
    if (inUl) {
      out.push("</ul>");
      inUl = false;
    }
    if (inOl) {
      out.push("</ol>");
      inOl = false;
    }
  };

  const flushTable = () => {
    if (!inTable) return;
    closeLists();
    const rows = tableRows.filter((r) => !/^\|?\s*:?-{3,}/.test(r.replace(/\|/g, "")));
    if (rows.length) {
      const htmlRows = rows.map((row, idx) => {
        const cells = row
          .replace(/^\|/, "")
          .replace(/\|$/, "")
          .split("|")
          .map((c) => c.trim());
        const tag = idx === 0 ? "th" : "td";
        return `<tr>${cells.map((c) => `<${tag}>${inlineMd(c)}</${tag}>`).join("")}</tr>`;
      });
      out.push(`<div class="table-wrap"><table class="data-table md-table">${htmlRows.join("")}</table></div>`);
    }
    tableRows = [];
    inTable = false;
  };

  for (const raw of lines) {
    const line = raw;
    const fenceMatch = line.match(/^%%FENCE(\d+)%%$/);
    if (fenceMatch) {
      flushTable();
      closeLists();
      out.push(fences[Number(fenceMatch[1])] || "");
      continue;
    }

    if (/^\s*\|/.test(line) && line.includes("|")) {
      closeLists();
      inTable = true;
      tableRows.push(line);
      continue;
    }
    if (inTable) flushTable();

    if (/^\s*[-*]\s+/.test(line)) {
      if (inOl) {
        out.push("</ol>");
        inOl = false;
      }
      if (!inUl) {
        out.push("<ul>");
        inUl = true;
      }
      out.push(`<li>${inlineMd(line.replace(/^\s*[-*]\s+/, ""))}</li>`);
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      if (inUl) {
        out.push("</ul>");
        inUl = false;
      }
      if (!inOl) {
        out.push("<ol>");
        inOl = true;
      }
      out.push(`<li>${inlineMd(line.replace(/^\s*\d+\.\s+/, ""))}</li>`);
      continue;
    }

    closeLists();

    if (/^###\s+/.test(line)) {
      out.push(`<h3>${inlineMd(line.replace(/^###\s+/, ""))}</h3>`);
      continue;
    }
    if (/^##\s+/.test(line)) {
      out.push(`<h2>${inlineMd(line.replace(/^##\s+/, ""))}</h2>`);
      continue;
    }
    if (/^#\s+/.test(line)) {
      out.push(`<h1>${inlineMd(line.replace(/^#\s+/, ""))}</h1>`);
      continue;
    }
    if (/^>\s?/.test(line)) {
      out.push(`<blockquote>${inlineMd(line.replace(/^>\s?/, ""))}</blockquote>`);
      continue;
    }
    if (/^---+$/.test(line.trim()) || /^\*\*\*+$/.test(line.trim())) {
      out.push("<hr />");
      continue;
    }
    if (!line.trim()) {
      out.push("");
      continue;
    }
    out.push(`<p>${inlineMd(line)}</p>`);
  }
  flushTable();
  closeLists();
  return `<div class="md-body">${out.join("\n")}</div>`;
}

/** @param {string} s */
function inlineMd(s) {
  let t = esc(s);
  t = t.replace(/`([^`]+)`/g, "<code>$1</code>");
  t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  t = t.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, (_m, label, href) => {
    // href is already HTML-escaped from esc(); reject anything that is not a plain http(s) URL.
    const raw = href
      .replace(/&amp;/g, "&")
      .replace(/&quot;/g, '"')
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">");
    if (!/^https?:\/\//i.test(raw) || /[\s"'<>`]/.test(raw)) {
      return _m;
    }
    return `<a href="${esc(raw)}" target="_blank" rel="noopener">${label}</a>`;
  });
  return t;
}

/**
 * Parse Prometheus text exposition into rows.
 * @param {string} text
 * @returns {{ name: string, labels: Record<string,string>, value: number }[]}
 */
export function parsePrometheusText(text) {
  /** @type {{ name: string, labels: Record<string,string>, value: number }[]} */
  const rows = [];
  for (const line of String(text || "").split("\n")) {
    const t = line.trim();
    if (!t || t.startsWith("#")) continue;
    const m = t.match(/^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{([^}]*)\})?\s+([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|NaN|Inf|\+Inf|-Inf)\s*$/);
    if (!m) continue;
    /** @type {Record<string,string>} */
    const labels = {};
    if (m[3]) {
      for (const part of m[3].split(",")) {
        const lm = part.trim().match(/^([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"$/);
        if (lm) labels[lm[1]] = lm[2].replace(/\\n/g, "\n").replace(/\\"/g, '"').replace(/\\\\/g, "\\");
      }
    }
    let value = Number(m[4]);
    if (m[4] === "+Inf" || m[4] === "Inf") value = Infinity;
    if (m[4] === "-Inf") value = -Infinity;
    if (m[4] === "NaN") value = NaN;
    rows.push({ name: m[1], labels, value });
  }
  return rows;
}

/** @param {{ name: string, labels: Record<string,string>, value: number }[]} rows @param {string} name */
export function sumMetric(rows, name) {
  return rows.filter((r) => r.name === name).reduce((a, r) => a + (Number.isFinite(r.value) ? r.value : 0), 0);
}

/** Copy text to clipboard */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(String(text || ""));
    return true;
  } catch {
    return false;
  }
}
