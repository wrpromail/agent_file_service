/**
 * Shared data table renderer.
 * @module js/components/table
 */

import { esc } from "../utils.js";

/**
 * @typedef {Object} Col
 * @property {string} key
 * @property {string} label
 * @property {string} [className]
 * @property {(row: Record<string, any>, i: number) => string} [render]  HTML cell
 */

/**
 * @param {{
 *   columns: Col[],
 *   rows: Record<string, any>[],
 *   empty?: string,
 *   rowAttrs?: (row: Record<string, any>, i: number) => string,
 *   tableClass?: string,
 * }} opts
 */
export function dataTable({ columns, rows, empty = "暂无数据", rowAttrs, tableClass = "" }) {
  if (!rows?.length) {
    return `<div class="state state-empty"><span class="state-text">${esc(empty)}</span></div>`;
  }
  const thead = columns
    .map((c) => `<th class="${esc(c.className || "")}">${esc(c.label)}</th>`)
    .join("");
  const tbody = rows
    .map((row, i) => {
      const attrs = rowAttrs ? rowAttrs(row, i) : "";
      const tds = columns
        .map((c) => {
          let cell;
          if (c.render) cell = c.render(row, i);
          else {
            const v = row[c.key];
            cell = v == null || v === "" ? "—" : esc(String(v));
          }
          return `<td class="${esc(c.className || "")}">${cell}</td>`;
        })
        .join("");
      return `<tr ${attrs}>${tds}</tr>`;
    })
    .join("");
  return `<div class="table-wrap">
    <table class="data-table${tableClass ? ` ${esc(tableClass)}` : ""}">
      <thead><tr>${thead}</tr></thead>
      <tbody>${tbody}</tbody>
    </table>
  </div>`;
}

