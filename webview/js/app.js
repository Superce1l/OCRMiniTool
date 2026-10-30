/* ============================================================
   app.js — OCR MiniTool 前端逻辑
   版面交互骨架沿用 STTMiniTool：侧栏视图切换 + SSE 进度 + 状态点。
   视图：识别（拖图/多选 → 识别 → Markdown 结果）/ 模型 / 设置。
   文案统一走 I18N.t()（语言文件 webview/locales/*.json，与后端共用）；
   语言切换经 I18N.onChange → retranslate() 重渲染动态区域。
   ============================================================ */
"use strict";

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const API = window.OCRApi;
const t = (k, p) => window.I18N.t(k, p);

const state = {
  view: "ocr",
  files: [],            // [File]
  running: false,
  mode: "merged",       // merged | batch
  lastResult: null,     // {text, pages, outPath, elapsed}
  lastBatchResult: null, // 批量模式整包结果（语言切换重渲染用）
  batchItems: null,     // 批量模式的逐文件结果
  viewIndex: 0,         // 批量模式当前查看的 item 下标
  curPage: 0,
  resultMode: "rendered",
  status: null,
  settings: { outFormat: "md" },
  knownExts: new Set(["jpg", "jpeg", "png", "bmp", "webp", "tif", "tiff", "pdf"]),
};

/* ── 工具 ─────────────────────────────────────────────── */
function fmtSize(n) {
  if (n == null) return "";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
  return (n / 1073741824).toFixed(2) + " GB";
}
function fmtTime(sec) {
  sec = Math.round(sec || 0);
  if (sec < 60) return t("time.sec", { s: sec });
  return t("time.minSec", { m: Math.floor(sec / 60), s: sec % 60 });
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function flash(msg) {
  logLine(msg);
}
const logLines = [];
function logLine(msg) {
  const ts = new Date().toLocaleTimeString(window.I18N.localeTag(), { hour12: false });
  logLines.push(`[${ts}] ${msg}`);
  if (logLines.length > 200) logLines.shift();
  const box = $("#ocr-log");
  box.hidden = false;
  box.innerHTML = logLines.map(l => `<div class="ln">${escapeHtml(l)}</div>`).join("");
  box.scrollTop = box.scrollHeight;
}

/* ── 视图切换 ─────────────────────────────────────────── */
function viewTitle(v) {
  return { ocr: t("view.ocr"), model: t("view.model"), settings: t("view.settings") }[v] || v;
}
function switchView(v) {
  state.view = v;
  $$(".nav-item").forEach(b => b.classList.toggle("active", b.dataset.view === v));
  $$(".view").forEach(s => s.classList.toggle("active", s.dataset.view === v));
  $("#view-title").textContent = viewTitle(v);
}
$("#nav").addEventListener("click", e => {
  const b = e.target.closest(".nav-item");
  if (b) switchView(b.dataset.view);
});
$("#model-status").addEventListener("click", () => switchView("model"));

/* ── 主题 / 缩放 ──────────────────────────────────────── */
const mqDark = window.matchMedia?.("(prefers-color-scheme: dark)");
function applyTheme(pref) {
  const dark = pref === "dark" || (pref === "system" && mqDark?.matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}
mqDark?.addEventListener?.("change", () => {
  if ((state.status?.theme || "light") === "system") applyTheme("system");
});
function applyScale(pct) {
  document.documentElement.style.fontSize = (16 * (pct || 100) / 100) + "px";
}

/* ── 语言切换：静态属性已由 I18N.applyAll 重刷，这里补动态区域 ── */
function retranslate() {
  switchView(state.view);
  renderFiles();
  applyStatus(state.status);
  if (state.batchItems && state.lastBatchResult) showBatchResult(state.lastBatchResult);
  else if (state.lastResult) showResultPages();
  refreshHistory();
}
window.I18N.onChange(retranslate);

/* ── 状态（轮询 + SSE 合流）────────────────────────────── */
function applyStatus(s) {
  if (!s) return;
  state.status = { ...state.status, ...s };
  const st = state.status;

  // 状态点
  const dot = $("#model-status");
  const txt = $("#model-status-t");
  dot.classList.remove("off", "loading", "error");
  dot.querySelector(".dot").style.background = "";
  if (st.modelState === "ready") {
    txt.textContent = t("status.ready", { dev: st.loadedDevice === "cuda" ? "CUDA" : "CPU" });
  } else if (st.modelState === "loading" || st.modelState === "downloading") {
    dot.classList.add("loading");
    txt.textContent = st.modelState === "loading" ? t("status.loading") : t("status.downloading");
  } else if (st.modelState === "error") {
    dot.classList.add("error");
    txt.textContent = t("status.error");
  } else if (st.modelState === "downloaded") {
    dot.classList.add("off");
    txt.textContent = t("status.notLoaded");
  } else {
    dot.classList.add("off");
    txt.textContent = t("status.notDownloaded");
  }
  dot.title = t("status.dirTip", { dir: st.modelDir || "" });

  $("#app-version").textContent = "v" + (st.version || "—");
  renderModelPage();
  renderRunButtons();
}

function renderRunButtons() {
  const st = state.status || {};
  const ready = st.modelState === "ready";
  $("#btn-run").disabled = !ready || state.running || !state.files.length;
  $("#btn-cancel").disabled = !state.running;
  $("#btn-open-dir").disabled = false;
}

/* ── 识别页：文件选择 ─────────────────────────────────── */
const drop = $("#drop");
const hiddenFile = $("#hidden-file");

drop.addEventListener("click", e => {
  if (e.target.closest("#btn-pick")) return;
  hiddenFile.click();
});
$("#btn-pick").addEventListener("click", () => hiddenFile.click());
hiddenFile.addEventListener("change", () => {
  addFiles([...hiddenFile.files]);
  hiddenFile.value = "";
});
["dragover", "dragenter"].forEach(ev =>
  drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("hot"); }));
["dragleave", "drop"].forEach(ev =>
  drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("hot"); }));
drop.addEventListener("drop", e => {
  addFiles([...(e.dataTransfer?.files || [])]);
});

function addFiles(list) {
  let rejected = 0;
  for (const f of list) {
    const ext = f.name.split(".").pop()?.toLowerCase() || "";
    // GIF 不支持（动图只读得到第一帧），MIME / 扩展名两条路都拦掉
    const ok = (f.type.startsWith("image/") && ext !== "gif")
      || state.knownExts.has(ext);
    if (!ok) { rejected++; continue; }
    state.files.push(f);
  }
  if (rejected) flash(t("msg.skippedFiles", { n: rejected }));
  renderFiles();
}

function renderFiles() {
  const box = $("#file-list");
  box.hidden = !state.files.length;
  box.innerHTML = state.files.map((f, i) => `
    <span class="file-chip" title="${escapeHtml(t("result.fileTip", { name: f.name, size: fmtSize(f.size) }))}">
      <span class="fn">${escapeHtml(f.name.length > 40 ? f.name.slice(0, 38) + "…" : f.name)}</span>
      <span class="meta">${fmtSize(f.size)}</span>
      <button class="x" data-i="${i}" title="${escapeHtml(t("result.remove"))}">×</button>
    </span>`).join("");
  renderRunButtons();
}
$("#file-list").addEventListener("click", e => {
  const x = e.target.closest(".x");
  if (!x) return;
  state.files.splice(+x.dataset.i, 1);
  renderFiles();
});

/* ── 识别模式 ─────────────────────────────────────────── */
$("#ocr-mode").addEventListener("click", e => {
  const b = e.target.closest("button");
  if (!b) return;
  state.mode = b.dataset.v;
  $$("#ocr-mode button").forEach(x => x.classList.toggle("on", x === b));
  flash(state.mode === "batch" ? t("msg.modeBatch") : t("msg.modeMerged"));
});

/* ── 识别 ─────────────────────────────────────────────── */
$("#btn-run").addEventListener("click", runOcr);
$("#btn-cancel").addEventListener("click", async () => {
  await API.cancel();
  flash(t("msg.cancelRequested"));
});

async function runOcr() {
  if (state.running || !state.files.length) return;
  state.running = true;
  renderRunButtons();
  $("#progress").hidden = false;
  setProgress(2, t("prog.uploading"));
  const prompt = $("#prompt-box").value.trim();
  try {
    const result = await API.recognize({
      files: state.files, prompt, mode: state.mode,
    });
    if (result && !result.cancelled) {
      if (result.mode === "batch" && result.items) {
        state.batchItems = result.items;
        state.lastBatchResult = result;
        state.viewIndex = result.items.findIndex(it => it.ok);
        showBatchResult(result);
        const okN = result.done ?? result.items.filter(i => i.ok).length;
        flash(t("msg.batchDone", {
          ok: okN, total: result.items.length, time: fmtTime(result.elapsed),
        }));
      } else {
        state.batchItems = null;
        state.lastBatchResult = null;
        state.lastResult = result;
        state.viewIndex = 0;
        showResult(result);
        flash(t("msg.done", { time: fmtTime(result.elapsed), path: result.outPath || "" }));
      }
    }
  } catch (e) {
    flash(t("msg.failed", { err: e.message || e }));
    setProgress(0, t("prog.failed"));
  } finally {
    state.running = false;
    renderRunButtons();
    setTimeout(() => { if (!state.running) $("#progress").hidden = true; }, 1500);
    refreshHistory();
  }
}

function setProgress(pct, status) {
  const p = Math.max(0, Math.min(100, Math.round((pct || 0) * 100)));
  $("#prog-bar").style.width = p + "%";
  $("#prog-pct").textContent = p + "%";
  if (status != null) $("#prog-status").textContent = status;
}

/* ── 结果渲染（轻量 Markdown）──────────────────────────── */
// 模型输出里的表格是原生 HTML（<table><tr><td>…），需原样通过而非转义
const HTML_BLOCK_RE = /^\s*<\/?(table|thead|tbody|tfoot|tr|th|td|ul|ol|li|p|div|br|figure|figcaption|h[1-6])\b/i;
const HTML_DANGEROUS_RE = /^\s*<\s*(script|iframe|object|embed|link|meta)\b/i;

function mdToHtml(md) {
  const lines = String(md).split(/\r?\n/);
  const out = [];
  let inCode = false, codeBuf = [], listType = null;

  const closeList = () => { if (listType) { out.push(`</${listType}>`); listType = null; } };

  const inline = s => escapeHtml(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*\n]+)\*/g, "<em>$1</em>")
    .replace(/!\[([^\]]*)\]\(([^)]+)\)/g, '<img alt="$1" src="$2">')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

  for (const raw of lines) {
    if (/^```/.test(raw.trim())) {
      if (inCode) { out.push(`<pre><code>${escapeHtml(codeBuf.join("\n"))}</code></pre>`); codeBuf = []; inCode = false; }
      else { closeList(); inCode = true; }
      continue;
    }
    if (inCode) { codeBuf.push(raw); continue; }

    const line = raw.trimEnd();
    let m;
    // 原生 HTML 块（模型输出的表格等）：原样插入；危险标签仍转义
    if (HTML_BLOCK_RE.test(line) && !HTML_DANGEROUS_RE.test(line)) {
      closeList();
      out.push(line);
      continue;
    }
    if ((m = line.match(/^(#{1,6})\s+(.*)/))) {
      closeList();
      const lv = Math.min(4, m[1].length);
      out.push(`<h${lv}>${inline(m[2])}</h${lv}>`);
    } else if ((m = line.match(/^\s*[-*+]\s+(.*)/))) {
      if (listType !== "ul") { closeList(); out.push("<ul>"); listType = "ul"; }
      out.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) {
      if (listType !== "ol") { closeList(); out.push("<ol>"); listType = "ol"; }
      out.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^\s*>\s?(.*)/))) {
      closeList();
      out.push(`<blockquote>${inline(m[1])}</blockquote>`);
    } else if (/^\s*(\|.*\|)\s*$/.test(line)) {
      closeList();
      const cells = line.slice(1, -1).split("|").map(c => c.trim());
      if (cells.every(c => /^:?-{2,}:?$/.test(c))) continue;   // 分隔行
      out.push(`<div class="tbl-row">${cells.map(c => `<span>${inline(c)}</span>`).join("")}</div>`);
    } else if (/^\s*(---+|\*\*\*+|___+)\s*$/.test(line)) {
      closeList();
      out.push("<hr>");
    } else if (!line.trim()) {
      closeList();
    } else {
      closeList();
      out.push(`<p>${inline(line)}</p>`);
    }
  }
  if (inCode) out.push(`<pre><code>${escapeHtml(codeBuf.join("\n"))}</code></pre>`);
  closeList();
  return out.join("\n");
}

function showResult(result) {
  $("#result").hidden = false;
  $("#btn-copy").disabled = false;
  $("#btn-save").disabled = false;
  $("#batch-list").hidden = true;
  $("#batch-list").innerHTML = "";
  showResultPages();
}

function renderResultText() {
  const r = state.lastResult;
  if (!r) return;
  const pages = r.pages || [];
  let text = r.text || "";
  if (state.curPage >= 0 && pages.length > 1) text = pages[state.curPage] || "";
  if (state.resultMode === "raw") {
    $("#md-render").hidden = true;
    const raw = $("#md-raw");
    raw.hidden = false;
    raw.value = text;
  } else {
    $("#md-raw").hidden = true;
    const box = $("#md-render");
    box.hidden = false;
    box.innerHTML = mdToHtml(text);
  }
}

/* ── 批量模式：逐文件结果 ─────────────────────────────────── */
function showBatchResult(result) {
  const items = result.items || [];
  $("#result").hidden = false;
  $("#btn-copy").disabled = false;
  $("#btn-save").disabled = false;
  const box = $("#batch-list");
  box.hidden = false;
  box.innerHTML = items.map((it, i) => {
    const cls = it.ok ? "ok" : (it.skipped ? "skip" : "fail");
    const badge = it.ok ? `<span class="chip chip-ok">${escapeHtml(t("chip.ok"))}</span>`
      : it.skipped ? `<span class="chip chip-muted">${escapeHtml(t("chip.skipped"))}</span>`
      : `<span class="chip chip-live">${escapeHtml(t("chip.fail"))}</span>`;
    const meta = it.ok
      ? escapeHtml(t("result.charsTime", { chars: it.chars, time: fmtTime(it.elapsed) }))
      : (it.error ? escapeHtml(it.error.slice(0, 80)) : "—");
    return `
      <button class="batch-item${i === state.viewIndex ? " on" : ""}" data-i="${i}">
        <span class="bn" title="${escapeHtml(it.name)}">${escapeHtml(it.name)}</span>
        ${badge}
        <span class="bm">${meta}</span>
        <span class="bx">${escapeHtml(t("result.view"))}</span>
      </button>`;
  }).join("");
  $("#result-meta").textContent = t("result.batchMeta", {
    done: result.done ?? items.filter(i => i.ok).length,
    total: items.length,
    time: fmtTime(result.elapsed),
  });
  viewBatchItem(state.viewIndex);
}

function viewBatchItem(i) {
  const items = state.batchItems || [];
  const it = items[i];
  if (!it || !it.ok) return;
  state.viewIndex = i;
  state.lastResult = { text: it.text, pages: it.pages || [it.text], outPath: it.outPath, elapsed: it.elapsed };
  state.curPage = 0;
  $$("#batch-list .batch-item").forEach(b => b.classList.toggle("on", +b.dataset.i === i));
  showResultPages();
}

/* 复用 showResult 的分页/渲染部分（不重置批量列表） */
function showResultPages() {
  const r = state.lastResult;
  const pages = r.pages || [];
  const tabs = $("#page-tabs");
  if (pages.length > 1) {
    tabs.hidden = false;
    tabs.innerHTML = pages.map((_, i) =>
      `<button class="page-tab${i === state.curPage ? " on" : ""}" data-i="${i}">${escapeHtml(t("result.pageN", { n: i + 1 }))}</button>`).join("") +
      `<button class="page-tab${state.curPage === -1 ? " on" : ""}" data-i="-1">${escapeHtml(t("result.allPages"))}</button>`;
  } else {
    tabs.hidden = true;
    tabs.innerHTML = "";
    state.curPage = 0;
  }
  $("#result-meta").textContent = t("result.pageMeta", {
    n: pages.length || 1, time: fmtTime(r.elapsed),
  });
  renderResultText();
}

$("#batch-list").addEventListener("click", e => {
  const b = e.target.closest(".batch-item");
  if (b) viewBatchItem(+b.dataset.i);
});

$("#page-tabs").addEventListener("click", e => {
  const b = e.target.closest(".page-tab");
  if (!b) return;
  state.curPage = +b.dataset.i;
  $$("#page-tabs .page-tab").forEach(x => x.classList.toggle("on", x === b));
  renderResultText();
});
$("#result-mode").addEventListener("click", e => {
  const b = e.target.closest("button");
  if (!b) return;
  state.resultMode = b.dataset.v;
  $$("#result-mode button").forEach(x => x.classList.toggle("on", x === b));
  renderResultText();
});

/* 复制 / 保存 */
$("#btn-copy").addEventListener("click", async () => {
  const r = state.lastResult;
  if (!r) return;
  const text = state.curPage >= 0 && (r.pages || []).length > 1
    ? r.pages[state.curPage] : r.text;
  try {
    await navigator.clipboard.writeText(text);
    flash(t("msg.copied"));
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta);
    ta.select(); document.execCommand("copy"); ta.remove();
    flash(t("msg.copied"));
  }
});
/* 纯文本导出：与后端 _md_to_plain 同规则的 JS 版 */
function mdToPlain(md) {
  return String(md || "")
    .replace(/<\/t[hd]>/gi, "\t")
    .replace(/<\/tr>/gi, "\n")
    .replace(/<[^>\n]{1,120}>/g, "")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\*([^*\n]+)\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/```[a-zA-Z0-9]*\n?/g, "")
    .replace(/^[\s>]*[-*+]\s+/gm, "")
    .replace(/[ \t]+\n/g, "\n")
    .trim();
}

$("#btn-save").addEventListener("click", () => {
  const r = state.lastResult;
  if (!r) return;
  const text = state.curPage >= 0 && (r.pages || []).length > 1
    ? r.pages[state.curPage] : r.text;
  const fmt = (state.settings && state.settings.outFormat) === "txt" ? "txt" : "md";
  const content = fmt === "txt" ? mdToPlain(text) : text;
  const base = (r.outPath ? r.outPath.split(/[\\/]/).pop() : "ocr-result")
    || "ocr-result";
  const blob = new Blob([content], {
    type: fmt === "txt" ? "text/plain;charset=utf-8" : "text/markdown;charset=utf-8",
  });
  const a = $("#download-link");
  a.href = URL.createObjectURL(blob);
  a.download = base.replace(/\.(md|txt)$/i, "") + "." + fmt;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
});
$("#btn-open-dir").addEventListener("click", () => API.openOutputDir());

/* ── 最近识别 ─────────────────────────────────────────── */
async function refreshHistory() {
  const { items } = await API.listHistory();
  const head = $("#history-head"), list = $("#history-list");
  if (!items.length) { head.hidden = true; list.hidden = true; return; }
  head.hidden = false; list.hidden = false;
  list.innerHTML = items.map(it => `
    <button class="history-item" data-name="${escapeHtml(it.name)}">
      <span class="hn">${escapeHtml(it.name)}</span>
      <span class="hm">${fmtSize(it.size)}</span>
      <span class="hx" data-del="${escapeHtml(it.name)}" role="button" aria-label="${escapeHtml(t("history.delete"))}" tabindex="0">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg>
      </span>
    </button>`).join("");
}
$("#history-list").addEventListener("click", async e => {
  const del = e.target.closest(".hx");
  if (del) {
    e.stopPropagation();
    const name = del.dataset.del;
    if (!confirm(t("msg.confirmDelete", { name }))) return;
    await API.deleteHistory(name);
    refreshHistory();
    return;
  }
  const item = e.target.closest(".history-item");
  if (!item) return;
  const r = await API.readHistory(item.dataset.name);
  if (r.ok) {
    state.lastResult = { text: r.text, pages: [r.text], outPath: r.name, elapsed: 0 };
    state.curPage = 0;
    showResult(state.lastResult);
    flash(t("msg.loadedHistory", { name: r.name }));
  }
});

/* ── 模型页 ───────────────────────────────────────────── */
function renderModelPage() {
  const st = state.status;
  if (!st) return;

  // 运行环境自检
  const torchOk = !!st.torch;
  const tfOk = !!st.transformers;
  const cells = [
    {
      title: t("env.deps"),
      items: [
        { label: "PyTorch", ok: torchOk, detail: st.torch || t("env.notInstalled") },
        { label: "Transformers", ok: tfOk, detail: st.transformers || t("env.notInstalled") },
      ],
    },
    {
      title: t("env.device"),
      items: [
        { label: "CPU", ok: true, detail: t("env.available") },
        { label: "CUDA GPU", ok: st.cuda, detail: st.cuda ? `${st.cudaName} · ${st.vramGb} GB` : t("env.noCuda") },
      ],
    },
  ];
  $("#env-panel").innerHTML = cells.map(c => `
    <div class="health-core">
      <div class="hc-title">${escapeHtml(c.title)}</div>
      ${c.items.map(i => `
        <div class="hc-item">
          <span class="hdot ${i.ok ? "hdot-green" : "hdot-yellow"}"></span>
          <span class="hi-label">${escapeHtml(i.label)}</span>
          <span class="hi-detail" title="${escapeHtml(i.detail)}">${escapeHtml(i.detail)}</span>
        </div>`).join("")}
    </div>`).join("");

  // 模型状态 chip
  const chip = $("#model-state-chip");
  const STATES = {
    absent: ["chip-muted", t("mstate.absent")],
    downloading: ["chip-warn", t("mstate.downloading")],
    downloaded: ["chip-warn", t("mstate.downloaded")],
    loading: ["chip-warn", t("mstate.loading")],
    ready: ["chip-ok", t("mstate.ready", { dev: st.loadedDevice === "cuda" ? "CUDA" : "CPU" })],
    error: ["chip-live", t("mstate.error")],
  };
  const [cls, label] = STATES[st.modelState] || STATES.absent;
  chip.className = "chip " + cls;
  chip.textContent = label;

  $("#model-meta").innerHTML = `
    <span>${escapeHtml(t("model.metaWeights"))}</span>
    <span>${escapeHtml(t("model.metaDir", { dir: shortPath(st.modelDir) }))}</span>
    ${st.mirror ? `<span>${escapeHtml(t("model.metaMirror", { m: st.mirror }))}</span>` : ""}`;

  // 按钮
  const dl = $("#btn-model-download"), ld = $("#btn-model-load");
  dl.disabled = ["downloading", "loading"].includes(st.modelState) || !!st.busy;
  dl.textContent = st.modelState === "downloaded" || st.modelState === "ready"
    ? t("model.redownload") : t("model.download");
  ld.disabled = st.modelState !== "downloaded" || !!st.busy;
  $("#btn-model-unload").hidden = st.modelState !== "ready";

  // 错误横幅
  const msg = $("#model-msg");
  if (st.modelState === "error" && st.modelError) {
    msg.innerHTML = `<div class="banner banner-warn" style="margin-top:12px">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
      <div>${escapeHtml(st.modelError)}</div></div>`;
  } else {
    msg.innerHTML = "";
  }
}
function shortPath(p, n = 46) {
  p = String(p || "");
  return p.length > n ? p.slice(0, 18) + "…" + p.slice(-24) : p;
}

$("#btn-model-download").addEventListener("click", async () => {
  $("#model-progress").hidden = false;
  setModelProgress(0, t("model.prepDownload"));
  const r = await API.startDownload();
  if (!r.ok) flash(r.error || t("msg.cannotDownload"));
});
$("#btn-model-load").addEventListener("click", async () => {
  $("#model-progress").hidden = false;
  setModelProgress(5, t("model.startLoad"));
  const r = await API.startLoad();
  if (!r.ok) { flash(r.error || t("msg.cannotLoad")); $("#model-progress").hidden = true; }
});
$("#btn-model-unload").addEventListener("click", async () => {
  // 卸载：暂以重启进程外的方式不支持 → 提示
  flash(t("msg.unloadHint"));
});

function setModelProgress(pct, status) {
  const p = Math.max(0, Math.min(100, Math.round((pct || 0) * 100)));
  $("#model-prog-bar").style.width = p + "%";
  $("#model-prog-pct").textContent = p + "%";
  if (status != null) $("#model-prog-status").textContent = status;
}

/* ── 设置页 ───────────────────────────────────────────── */
// 语言下拉选项：优先用后端发现的 languages（扫 locales 目录），
// 没有则退回 I18N.LANGUAGES 内置列表；选项按 code 去重后只在变化时重建
let langOptionsSig = "";
function renderLangOptions(langs) {
  const list = Array.isArray(langs) && langs.length ? langs : window.I18N.LANGUAGES;
  const sig = list.map(l => l.code).join(",");
  if (sig === langOptionsSig) return;
  langOptionsSig = sig;
  $("#set-lang").innerHTML = list.map(l =>
    `<option value="${escapeHtml(l.code)}">${escapeHtml(l.name)}</option>`).join("");
}
function fillSettings(s) {
  if (!s) return;
  state.settings = s;
  $("#set-model-dir").value = s.modelDir || "";
  $("#set-mirror").value = s.mirror || "";
  $("#set-scale").value = String(s.scale || 100);
  $$("#set-device button").forEach(b => b.classList.toggle("on", b.dataset.v === (s.devicePref || "auto")));
  $$("#set-theme button").forEach(b => b.classList.toggle("on", b.dataset.v === (s.theme || "light")));
  $$("#set-outfmt button").forEach(b => b.classList.toggle("on", b.dataset.v === (s.outFormat || "md")));
  renderLangOptions(s.languages);
  $("#set-lang").value = s.lang || "zh";
  applyTheme(s.theme || "light");
  applyScale(s.scale || 100);
  // 语言变化：加载对应语言包并重渲染（后端文案由 set_settings 同步）
  const wantLang = s.lang || "zh";
  if (window.I18N.lang !== wantLang) window.I18N.setLang(wantLang);
}
let saveTimer = null;
function pushSettings(patch) {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    const s = await API.setSettings(patch);
    fillSettings(s);
    const st = await API.getStatus();
    applyStatus(st);
    flash(t("msg.settingsSaved"));
  }, 250);
}
$("#set-model-dir").addEventListener("change", () => pushSettings({ modelDir: $("#set-model-dir").value.trim() }));
$("#btn-model-dir-browse").addEventListener("click", async () => {
  const r = await API.browseFolder($("#set-model-dir").value);
  if (r.ok && r.path) { $("#set-model-dir").value = r.path; pushSettings({ modelDir: r.path }); }
});
$("#set-mirror").addEventListener("change", () => pushSettings({ mirror: $("#set-mirror").value.trim() }));
$("#set-outfmt").addEventListener("click", e => {
  const b = e.target.closest("button");
  if (b) pushSettings({ outFormat: b.dataset.v });
});
$("#set-scale").addEventListener("change", () => pushSettings({ scale: +$("#set-scale").value }));
$("#set-device").addEventListener("click", e => {
  const b = e.target.closest("button");
  if (b) pushSettings({ devicePref: b.dataset.v });
});
$("#set-theme").addEventListener("click", e => {
  const b = e.target.closest("button");
  if (b) pushSettings({ theme: b.dataset.v });
});
$("#set-lang").addEventListener("change", () => pushSettings({ lang: $("#set-lang").value }));

/* ── SSE 事件 ─────────────────────────────────────────── */
API.on("status", s => applyStatus(s));
API.on("progress", ({ pct, status }) => {
  $("#progress").hidden = false;
  setProgress(pct, status);
});
API.on("dl-progress", ({ pct, status }) => {
  $("#model-progress").hidden = false;
  setModelProgress(pct, status);
});
API.on("load-progress", ({ pct, status }) => {
  $("#model-progress").hidden = false;
  setModelProgress(pct, status);
});

/* ── 关闭兜底（浏览器/Edge 模式；native=1 时跳过，由原生弹窗负责）─── */
if (!new URLSearchParams(location.search).get("native")) {
  window.addEventListener("beforeunload", e => {
    if (state.running || state.status?.busy) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
}

/* ── 启动 ─────────────────────────────────────────────── */
(async function init() {
  await API.ready;
  try {
    const s = await API.getSettings();
    await window.I18N.setLang(s.lang || "zh");   // 先定语言再刷界面
    fillSettings(s);
    applyStatus(await API.getStatus());
  } catch (e) {
    console.warn("init status failed", e);
  }
  refreshHistory();
  flash(API.mode === "web" ? t("msg.readyWeb") : t("msg.readyDemo"));
})();
