/**
 * Simple tab strip controller.
 * @module js/components/tabs
 */

/**
 * @param {HTMLElement} root  container with .tabs and .tab-panels
 * @param {{ onChange?: (id: string) => void, initial?: string }} [opts]
 */
export function bindTabs(root, opts = {}) {
  const tabBar = root.querySelector(".tabs");
  const panels = root.querySelectorAll("[data-tab-panel]");
  if (!tabBar) return { activate: () => {} };

  const buttons = /** @type {HTMLButtonElement[]} */ ([...tabBar.querySelectorAll(".tab[data-tab]")]);

  function activate(id) {
    const target = id || buttons[0]?.dataset.tab;
    if (!target) return;
    for (const btn of buttons) {
      const on = btn.dataset.tab === target;
      btn.classList.toggle("is-active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    }
    for (const panel of panels) {
      const el = /** @type {HTMLElement} */ (panel);
      const on = el.dataset.tabPanel === target;
      el.hidden = !on;
    }
    if (opts.onChange) opts.onChange(target);
  }

  for (const btn of buttons) {
    btn.addEventListener("click", () => activate(btn.dataset.tab || ""));
  }

  activate(opts.initial || buttons.find((b) => b.classList.contains("is-active"))?.dataset.tab || "");
  return { activate };
}
