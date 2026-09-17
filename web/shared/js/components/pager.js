/**
 * Simple prev/next pager.
 * @module js/components/pager
 */

import { esc } from "../utils.js";

/**
 * @param {HTMLElement} el
 * @param {{ total: number, limit: number, offset: number, onChange: (offset: number) => void }} state
 */
export function renderPager(el, state) {
  const { total, limit, offset, onChange } = state;
  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + limit, total);
  const canPrev = offset > 0;
  const canNext = offset + limit < total;

  el.innerHTML = `
    <span>显示 ${from}–${to} / 共 ${esc(String(total))}</span>
    <div class="pager-actions">
      <button type="button" class="secondary" data-act="prev" ${canPrev ? "" : "disabled"}>上一页</button>
      <button type="button" class="secondary" data-act="next" ${canNext ? "" : "disabled"}>下一页</button>
    </div>
  `;
  el.querySelector('[data-act="prev"]')?.addEventListener("click", () => {
    onChange(Math.max(0, offset - limit));
  });
  el.querySelector('[data-act="next"]')?.addEventListener("click", () => {
    onChange(offset + limit);
  });
}
