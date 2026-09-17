/**
 * Shared page chrome primitives — header, panel, empty/error/loading.
 * @module js/components/page
 */

import { esc } from "../utils.js";

/**
 * @param {{ title: string, sub?: string, actionsHtml?: string }} opts
 */
export function pageHeader({ title, sub = "", actionsHtml = "" }) {
  return `
    <div class="page-header">
      <div class="page-header-text">
        <h1 class="page-title">${esc(title)}</h1>
        ${sub ? `<p class="page-sub">${sub}</p>` : ""}
      </div>
      ${actionsHtml ? `<div class="page-header-actions">${actionsHtml}</div>` : ""}
    </div>
  `;
}

/**
 * @param {{
 *   title?: string,
 *   titleHtml?: string,
 *   metaHtml?: string,
 *   actionsHtml?: string,
 *   body?: string,
 *   bodyId?: string,
 *   tight?: boolean,
 *   className?: string,
 * }} opts
 */
export function panel(opts = {}) {
  const title =
    opts.titleHtml != null
      ? opts.titleHtml
      : opts.title
        ? `<span class="panel-title">${esc(opts.title)}</span>`
        : "";
  const hasHd = title || opts.metaHtml || opts.actionsHtml;
  const bodyCls = `panel-bd${opts.tight ? " tight" : ""}`;
  const bodyAttr = opts.bodyId ? ` id="${esc(opts.bodyId)}"` : "";
  return `
    <section class="panel${opts.className ? ` ${esc(opts.className)}` : ""}">
      ${
        hasHd
          ? `<div class="panel-hd">
        ${title}
        <div class="panel-hd-right">
          ${opts.metaHtml || ""}
          ${opts.actionsHtml || ""}
        </div>
      </div>`
          : ""
      }
      <div class="${bodyCls}"${bodyAttr}>${opts.body ?? ""}</div>
    </section>
  `;
}

/** @param {string} [msg] */
export function loadingState(msg = "加载中…") {
  return `<div class="state state-loading" role="status">
    <span class="state-spinner" aria-hidden="true"></span>
    <span class="state-text">${esc(msg)}</span>
  </div>`;
}

/** @param {string} [msg] */
export function emptyState(msg = "暂无数据") {
  return `<div class="state state-empty" role="status">
    <span class="state-icon" aria-hidden="true">∅</span>
    <span class="state-text">${esc(msg)}</span>
  </div>`;
}

/** @param {string} msg */
export function errorState(msg) {
  return `<div class="state state-error" role="alert">
    <span class="state-icon" aria-hidden="true">!</span>
    <span class="state-text">${esc(String(msg || "请求失败"))}</span>
  </div>`;
}

/**
 * @param {Array<[string, string]>} pairs  [label, valueHtml] — value may be HTML
 */
export function metaGrid(pairs) {
  const rows = (pairs || [])
    .map(
      ([k, v]) =>
        `<dt>${esc(k)}</dt><dd>${v == null || v === "" ? "—" : v}</dd>`,
    )
    .join("");
  return `<dl class="meta-grid">${rows}</dl>`;
}

/**
 * Filter toolbar row.
 * @param {string} innerHtml
 */
export function toolbar(innerHtml) {
  return `<div class="toolbar">${innerHtml}</div>`;
}
