/**
 * Upload sandbox page.
 * @module js/pages/upload-page
 */

import { uploadFile, uploadFromUrl } from "../api.js";
import { statusChip } from "../components/badge.js";
import { errorState, pageHeader, panel } from "../components/page.js";
import { toast } from "../components/toast.js";
import { esc, formatBytes } from "../utils.js";

/** @type {File|null} */
let selected = null;

export function initUpload() {
  const root = document.getElementById("pageRoot");
  if (!root) return;

  root.innerHTML = `
    ${pageHeader({
      title: "上传沙箱",
      sub: "multipart 上传或 from-url · 成功后可跳转详情",
    })}
    <div class="split-2">
      ${panel({
        title: "本地文件",
        body: `
          <div class="file-drop" id="dropZone" tabindex="0">
            <div id="dropLabel">拖拽文件到此处，或点击选择</div>
            <div class="muted" id="dropMeta" style="margin-top:0.35rem"></div>
            <input type="file" id="fileInput" hidden />
          </div>
          <div class="form-grid" style="margin-top:1rem">
            <div class="form-field"><label>session_id</label><input id="uSession" /></div>
            <div class="form-field"><label>agent_id</label><input id="uAgent" /></div>
            <div class="form-field"><label>uploaded_by</label><input id="uBy" /></div>
            <div class="form-field"><label>expires_at (ISO)</label><input id="uExpires" placeholder="2026-12-31T00:00:00Z" /></div>
            <div class="form-field"><label>archive_password</label><input id="uPass" type="password" autocomplete="off" /></div>
            <div class="form-field">
              <label>auto_parse</label>
              <select id="uAuto"><option value="true" selected>true</option><option value="false">false</option></select>
            </div>
            <div class="form-field full"><label>custom metadata (JSON)</label>
              <textarea id="uMeta" rows="3" placeholder='{"source":"dev-console"}'></textarea>
            </div>
          </div>
          <div style="margin-top:1rem;display:flex;gap:0.5rem">
            <button type="button" id="btnUpload">上传</button>
            <button type="button" class="secondary" id="btnClear">清除</button>
          </div>
        `,
      })}
      ${panel({
        title: "从 URL 导入",
        body: `
          <div class="form-grid">
            <div class="form-field full"><label>url</label><input id="urlInput" placeholder="https://…" /></div>
            <div class="form-field"><label>filename（可选）</label><input id="urlName" /></div>
            <div class="form-field"><label>session_id</label><input id="urlSession" /></div>
            <div class="form-field"><label>agent_id</label><input id="urlAgent" /></div>
            <div class="form-field">
              <label>auto_parse</label>
              <select id="urlAuto"><option value="true" selected>true</option><option value="false">false</option></select>
            </div>
          </div>
          <div style="margin-top:1rem">
            <button type="button" id="btnUrl">导入</button>
          </div>
        `,
      })}
    </div>
    ${panel({
      title: "最近结果",
      bodyId: "resultPanel",
      body: `<div class="muted">上传后显示 reference_id / auto_parse / archive</div>`,
    })}
  `;

  const drop = document.getElementById("dropZone");
  const input = /** @type {HTMLInputElement} */ (document.getElementById("fileInput"));

  drop?.addEventListener("click", () => input?.click());
  drop?.addEventListener("dragover", (e) => {
    e.preventDefault();
    drop.classList.add("is-drag");
  });
  drop?.addEventListener("dragleave", () => drop.classList.remove("is-drag"));
  drop?.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("is-drag");
    const f = e.dataTransfer?.files?.[0];
    if (f) setFile(f);
  });
  input?.addEventListener("change", () => {
    if (input.files?.[0]) setFile(input.files[0]);
  });

  document.getElementById("btnClear")?.addEventListener("click", () => {
    selected = null;
    if (input) input.value = "";
    setText("dropLabel", "拖拽文件到此处，或点击选择");
    setText("dropMeta", "");
  });

  document.getElementById("btnUpload")?.addEventListener("click", onUpload);
  document.getElementById("btnUrl")?.addEventListener("click", onUrl);
}

/** @param {File} f */
function setFile(f) {
  selected = f;
  setText("dropLabel", f.name);
  setText("dropMeta", `${formatBytes(f.size)} · ${f.type || "unknown type"}`);
}

async function onUpload() {
  if (!selected) {
    toast("请先选择文件", { type: "err" });
    return;
  }
  const fields = {
    session_id: val("uSession"),
    agent_id: val("uAgent"),
    uploaded_by: val("uBy"),
    expires_at: val("uExpires"),
    archive_password: val("uPass"),
    auto_parse: val("uAuto"),
    metadata: val("uMeta") || undefined,
  };
  if (fields.metadata) {
    try {
      JSON.parse(fields.metadata);
    } catch {
      toast("metadata 不是合法 JSON", { type: "err" });
      return;
    }
  }
  try {
    toast("上传中…");
    const res = await uploadFile(selected, fields);
    showResult(res);
    toast("上传成功", { type: "ok" });
  } catch (err) {
    showError(err);
  }
}

async function onUrl() {
  const url = val("urlInput");
  if (!url) {
    toast("请填写 URL", { type: "err" });
    return;
  }
  try {
    toast("导入中…");
    const res = await uploadFromUrl({
      url,
      filename: val("urlName") || undefined,
      session_id: val("urlSession") || undefined,
      agent_id: val("urlAgent") || undefined,
      auto_parse: val("urlAuto") !== "false",
    });
    showResult(res);
    toast("导入成功", { type: "ok" });
  } catch (err) {
    showError(err);
  }
}

/** @param {any} res */
function showResult(res) {
  const el = document.getElementById("resultPanel");
  if (!el) return;
  const arch = res.archive;
  el.innerHTML = `
    <dl class="meta-grid">
      <dt>reference_id</dt><dd class="mono"><a href="/file.html?id=${esc(res.reference_id)}">${esc(res.reference_id)}</a></dd>
      <dt>content_hash</dt><dd class="mono">${esc(res.content_hash)}</dd>
      <dt>deduplicated</dt><dd>${res.deduplicated ? statusChip("ok") : "false"}</dd>
      <dt>parse_status</dt><dd>${statusChip(res.parse_status || "—")}</dd>
      <dt>auto_parse</dt><dd>requested=${esc(String(res.auto_parse_requested))} · applied=${esc(String(res.auto_parse_applied))}
        ${res.auto_parse_skip_reason ? `<div class="muted">${esc(res.auto_parse_skip_reason)} · ${esc(res.auto_parse_message || "")}</div>` : ""}
      </dd>
      ${
        arch
          ? `<dt>archive</dt><dd>${statusChip(arch.status)} · ${esc(arch.format || "")}
            ${arch.message ? `<div class="muted">${esc(arch.message)}</div>` : ""}
            ${
              (arch.children || []).length
                ? `<ul style="margin:0.4rem 0 0 1rem">${arch.children
                    .map(
                      (c) =>
                        `<li><a href="/file.html?id=${esc(c.reference_id)}">${esc(c.filename)}</a> ${statusChip(c.parse_status)}</li>`,
                    )
                    .join("")}</ul>`
                : ""
            }
          </dd>`
          : ""
      }
    </dl>
    <div style="margin-top:0.75rem">
      <a class="btn" href="/file.html?id=${esc(res.reference_id)}">打开详情</a>
    </div>
  `;
}

function showError(err) {
  const el = document.getElementById("resultPanel");
  if (el) el.innerHTML = errorState(err.message || String(err));
  toast(err.message || String(err), { type: "err" });
}

function val(id) {
  return /** @type {HTMLInputElement|HTMLSelectElement|HTMLTextAreaElement|null} */ (
    document.getElementById(id)
  )?.value?.trim() || "";
}

function setText(id, t) {
  const el = document.getElementById(id);
  if (el) el.textContent = t;
}
