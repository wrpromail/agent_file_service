/**
 * Status chips.
 * @module js/components/badge
 */

import { esc, parseStatusChipClass } from "../utils.js";

/** @param {string} status */
export function statusChip(status) {
  const s = String(status || "—");
  const cls = parseStatusChipClass(s);
  return `<span class="chip ${cls}">${esc(s)}</span>`;
}

/** @param {string} profile @param {string} status */
export function profileChip(profile, status) {
  return `<span class="chip ${parseStatusChipClass(status)}" title="${esc(profile)}">${esc(profile)}: ${esc(status || "—")}</span>`;
}
