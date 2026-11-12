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
  files: [],            // [File] 浏览器上传的文件
  pathFiles: [],        // [{path,name,size,ext}] 目录扫描加入的文件（走 JSON 直读路径）
  running: false,
  mode: "merged",       // merged | batch
  lastResult: null,     // {text, pages, outPath, elapsed}
  lastBatchResult: null, // 批量模式整包结果（语言切换重渲染用）
  batchItems: null,     // 批量模式的逐文件结果
  batchSource: "",      // 本轮批量的输入来源："files" | "paths"（失败重试按来源匹配）
  viewIndex: 0,         // 批量模式当前查看的 item 下标
  curPage: 0,
  resultMode: "rendered",
  status: null,
  env: null,            // /api/env 推理引擎组件状态（硬件检测 + 菜单 + 安装态）
  envFlavor: null,      // 卡片上当前点选的组件 key（null＝跟随 selected）
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
// 历史文件的修改时间：按当前界面语言格式化（B12；后端 list_history 已给 mtime 秒）
function fmtDateTime(sec) {
  const n = Number(sec);
  if (!n) return "";
  try {
    return new Date(n * 1000).toLocaleString(window.I18N.localeTag(), {
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hour12: false,
    });
  } catch { return ""; }
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ── 结果区 HTML 片段消毒（B1）────────────────────────────
   模型输出的表格等是原生 HTML，必须原样渲染；旧实现把整行直接写进
   innerHTML，等于把 OCR 文本（可被一张图片控制）当作代码执行。
   这里改为「只识别、不复制」：按白名单重建标签与属性，文本节点统一转义；
   未列入白名单的标签丢弃（其文本保留），script/style 等非文本容器整段丢弃。
   属性值一律不透传：数字属性校验取值，URL 属性校验协议。 */
const HTML_ALLOWED_TAGS = new Set([
  "table", "thead", "tbody", "tfoot", "tr", "th", "td",
  "ul", "ol", "li", "p", "div", "br", "figure", "figcaption",
  "h1", "h2", "h3", "h4", "h5", "h6",
  "code", "pre", "blockquote", "strong", "em", "img", "a",
]);
const HTML_VOID_TAGS = new Set(["br", "img"]);
// 内容不是人类可读文本的容器：整段丢弃（否则 <script> 源码会被当正文显示）
const HTML_DROP_TAGS = new Set([
  "script", "style", "template", "noscript", "iframe", "object", "embed",
  "svg", "math", "link", "meta", "title", "textarea", "select", "option",
  "form", "input", "button", "base",
]);
const HTML_TAG_RE = /^<\s*(\/?)\s*([a-zA-Z][a-zA-Z0-9]*)((?:"[^"]*"|'[^']*'|[^<>"'])*)>/;
const HTML_ATTR_RE = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;
const LINK_SCHEME_RE = /^(?:https?:\/\/|mailto:)/i;
const REL_PATH_RE = /^[A-Za-z0-9._~\-/%#?=&+,;@!$()*[\]]+$/;
const DATA_IMG_RE = /^data:image\/[a-z0-9.+-]+[;,]?/i;

// 解码常见 HTML 实体（模型表格里的 &nbsp; / &amp;），解码后再统一转义
function decodeEntities(s) {
  return String(s)
    .replace(/&nbsp;/gi, "\u00a0")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, "\"")
    .replace(/&#39;|&apos;/gi, "'")
    .replace(/&#(\d{1,6});/g, (m, d) => {
      const n = parseInt(d, 10);
      return n > 0 && n <= 0x10ffff ? String.fromCodePoint(n) : m;
    });
}
const textNode = s => escapeHtml(decodeEntities(s));
// 注意：URL 属性先解码实体再校验协议（否则 &#106;avascript: 这类编码能绕过校验），
// 校验通过的值只做 HTML 转义即可。
const attrValue = s => escapeHtml(String(s));

function parseHtmlAttrs(raw) {
  const out = {};
  let m;
  HTML_ATTR_RE.lastIndex = 0;
  while ((m = HTML_ATTR_RE.exec(raw))) {
    const name = m[1].toLowerCase();
    if (Object.prototype.hasOwnProperty.call(out, name)) continue;
    out[name] = m[2] !== undefined ? m[2]
      : (m[3] !== undefined ? m[3] : (m[4] !== undefined ? m[4] : ""));
  }
  return out;
}

function safeAttrString(tag, attrs) {
  const parts = [];
  for (const name of Object.keys(attrs)) {
    const raw = String(attrs[name]);
    if (name === "colspan" || name === "rowspan") {
      if (tag !== "th" && tag !== "td") continue;
      if (!/^\d{1,3}$/.test(raw)) continue;
      const n = parseInt(raw, 10);
      if (n >= 1 && n <= 99) parts.push(`${name}="${n}"`);
    } else if (name === "href" && tag === "a") {
      const v = decodeEntities(raw).trim();
      if (LINK_SCHEME_RE.test(v) && !/[\s\u0000-\u001f\u007f]/.test(v)) {
        parts.push(`href="${attrValue(v)}"`);
      }
    } else if (name === "src" && tag === "img") {
      const v = decodeEntities(raw).trim();
      if (!v || /[\u0000-\u001f\u007f]/.test(v)) continue;
      if (LINK_SCHEME_RE.test(v)) parts.push(`src="${attrValue(v)}"`);
      else if (DATA_IMG_RE.test(v) && !/[\s<>]/.test(v)) parts.push(`src="${attrValue(v)}"`);
      else if (REL_PATH_RE.test(v) && !v.startsWith("//")) parts.push(`src="${attrValue(v)}"`);
    }
    // 其余属性（on*、style、class、id、srcset…）一律丢弃
  }
  if (tag === "a" && parts.some(p => p.startsWith("href="))) {
    parts.push("target=\"_blank\"", "rel=\"noopener noreferrer\"");
  }
  return parts.length ? " " + parts.join(" ") : "";
}

function sanitizeHtmlFragment(html) {
  const src = String(html == null ? "" : html);
  const out = [];
  const stack = [];
  let i = 0;
  while (i < src.length) {
    const lt = src.indexOf("<", i);
    if (lt < 0) { out.push(textNode(src.slice(i))); break; }
    if (lt > i) out.push(textNode(src.slice(i, lt)));
    if (src.startsWith("<!--", lt)) {          // 注释整段丢弃
      const end = src.indexOf("-->", lt + 4);
      i = end < 0 ? src.length : end + 3;
      continue;
    }
    if (src.startsWith("<!", lt) || src.startsWith("<?", lt)) {
      const end = src.indexOf(">", lt + 2);
      i = end < 0 ? src.length : end + 1;
      continue;
    }
    const m = HTML_TAG_RE.exec(src.slice(lt));
    if (!m) { out.push("&lt;"); i = lt + 1; continue; }   // 裸 '<' 当文本
    const closing = m[1] === "/";
    const tag = m[2].toLowerCase();
    i = lt + m[0].length;
    if (closing) {
      if (!HTML_ALLOWED_TAGS.has(tag) || HTML_VOID_TAGS.has(tag)) continue;
      const at = stack.lastIndexOf(tag);
      if (at < 0) continue;
      while (stack.length > at) out.push(`</${stack.pop()}>`);
      continue;
    }
    if (HTML_DROP_TAGS.has(tag)) {
      // 整段丢弃到对应闭合标签（找不到就丢到结尾）；不解析其内部
      const close = new RegExp(`</\\s*${tag}\\s*>`, "i");
      const cm = close.exec(src.slice(i));
      i = cm ? i + cm.index + cm[0].length : src.length;
      continue;
    }
    if (!HTML_ALLOWED_TAGS.has(tag)) continue;   // 标签丢弃，文本保留
    out.push(`<${tag}${safeAttrString(tag, parseHtmlAttrs(m[3] || ""))}>`);
    if (!HTML_VOID_TAGS.has(tag)) stack.push(tag);
  }
  while (stack.length) out.push(`</${stack.pop()}>`);
  return out.join("");
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
  if (v === "model") loadEnv();   // 硬件检测有成本，仅在进入模型页时拉取
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
  // 主题偏好存在设置里（get_status 不带 theme，见 F18），不要读 state.status
  if ((state.settings?.theme || "light") === "system") applyTheme("system");
});

/* 界面缩放（B4）：CSS 里全站都是 px，改根字号不会缩放任何控件。
   Chromium/WebView2 支持 zoom 时对 .app 整体等比缩放，并按 innerHeight/zoom
   反算高度，保证缩放后仍正好铺满窗口；不支持时回退到根字号（文字生效）。 */
const MIN_SCALE = 25, MAX_SCALE = 200;
// 优先用 style 属性存在性判断（Chromium 一直认 zoom，哪怕旧版 CSS.supports 不报）；
// 都不认时退回字号缩放，行为与旧版一致。
const CAN_ZOOM = (() => {
  try {
    if (document.documentElement && "zoom" in document.documentElement.style) return true;
    return typeof CSS !== "undefined" && !!CSS.supports && CSS.supports("zoom", "1");
  } catch { return false; }
})();
let curScale = 100;
function syncAppHeight() {
  const app = $(".app");
  if (!app || !CAN_ZOOM) return;
  app.style.height = Math.max(1, Math.round(window.innerHeight / (curScale / 100))) + "px";
}
function applyScale(pct) {
  const n = Math.min(MAX_SCALE, Math.max(MIN_SCALE, Math.round(+pct || 100)));
  curScale = n;
  const root = document.documentElement;
  if (CAN_ZOOM) {
    root.style.setProperty("--ui-zoom", String(n / 100));
    root.style.fontSize = "";
    syncAppHeight();
  } else {
    root.style.setProperty("--ui-zoom", "1");
    root.style.fontSize = (16 * n / 100) + "px";
  }
}
window.addEventListener("resize", () => { if (CAN_ZOOM) syncAppHeight(); });

/* ── 语言切换：静态属性已由 I18N.applyAll 重刷，这里补动态区域 ── */
function retranslate() {
  switchView(state.view);
  renderFiles();
  applyStatus(state.status);
  // Python 探测结果是用 t() 即时生成的，语言切换后必须重渲染（F/I2）
  if (pyUI.lastProbe) renderProbeResult(pyUI.lastProbe);
  if (state.batchItems && state.lastBatchResult) showBatchResult(state.lastBatchResult);
  else if (state.lastResult) {
    renderResultWarn(resultWarnLines(state.lastResult));   // 提示条文案也要跟随语言
    showResultPages();
  }
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
  const hasInput = state.files.length + state.pathFiles.length > 0;
  $("#btn-run").disabled = !ready || state.running || !hasInput;
  $("#btn-cancel").disabled = !state.running;
  $("#btn-open-dir").disabled = false;
}

/* ── 识别页：文件选择 ─────────────────────────────────── */
const drop = $("#drop");
const hiddenFile = $("#hidden-file");

drop.addEventListener("click", e => {
  // 拖放区内的按钮各自处理点击，不要把「选文件夹」也当成「选文件」
  if (e.target.closest("#btn-pick") || e.target.closest("#btn-pick-dir")) return;
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
// 文档级兜底（B14）：文件落在拖放区之外时，Chromium 默认会导航到该文件，
// 整个界面连同未保存结果一起丢失。这里统一拦住并提示正确落点。
document.addEventListener("dragover", e => {
  if (!drop.contains(e.target)) e.preventDefault();
});
document.addEventListener("drop", e => {
  if (drop.contains(e.target)) return;
  e.preventDefault();
  flash(t("msg.dropOutside"));
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

function clipName(name) {
  name = String(name || "");
  return name.length > 40 ? name.slice(0, 38) + "…" : name;
}

function renderFiles() {
  const box = $("#file-list");
  const uploadChips = state.files.map((f, i) => `
    <span class="file-chip" title="${escapeHtml(t("result.fileTip", { name: f.name, size: fmtSize(f.size) }))}">
      <span class="fn">${escapeHtml(clipName(f.name))}</span>
      <span class="meta">${fmtSize(f.size)}</span>
      <button class="x" data-kind="file" data-i="${i}" title="${escapeHtml(t("result.remove"))}">×</button>
    </span>`).join("");
  // 目录来源的文件用虚线 chip 区分，title 显示完整路径
  const pathChips = state.pathFiles.map((p, i) => `
    <span class="file-chip dir" title="${escapeHtml(t("result.fileTip", { name: p.path || p.name, size: fmtSize(p.size) }))}">
      <span class="fn">${escapeHtml(clipName(p.name))}</span>
      <span class="meta">${fmtSize(p.size)}</span>
      <button class="x" data-kind="path" data-i="${i}" title="${escapeHtml(t("result.remove"))}">×</button>
    </span>`).join("");
  const total = state.files.length + state.pathFiles.length;
  box.hidden = !total;
  box.innerHTML = uploadChips + pathChips;
  const summary = $("#file-summary");
  summary.hidden = !total;
  if (total) {
    const bytes = state.files.reduce((a, f) => a + (f.size || 0), 0)
      + state.pathFiles.reduce((a, p) => a + (p.size || 0), 0);
    $("#file-summary-text").textContent = t("files.summary", { n: total, size: fmtSize(bytes) });
  }
  renderRunButtons();
}
$("#file-list").addEventListener("click", e => {
  const x = e.target.closest(".x");
  if (!x) return;
  const i = +x.dataset.i;
  if (x.dataset.kind === "path") state.pathFiles.splice(i, 1);
  else state.files.splice(i, 1);
  renderFiles();
});
$("#btn-clear-files").addEventListener("click", () => {
  state.files = [];
  state.pathFiles = [];
  renderFiles();
  flash(t("msg.filesCleared"));
});

/* ── 目录识别（S3）：选目录 → scan-dir → 加入待识别列表（JSON 直读路径）── */
const DIR_SCAN_MAX = 200;   // 与后端 MAX_RECOGNIZE_PATHS 对齐，避免扫描后一次识别被拒
async function pickDirectory() {
  if (state.running) return;
  if (API.mode !== "web") { flash(t("msg.scanNeedsBackend")); return; }
  const room = DIR_SCAN_MAX - state.pathFiles.length;
  if (room <= 0) { flash(t("msg.dirLimitReached", { n: DIR_SCAN_MAX })); return; }
  const r = await API.browseFolder("");
  if (!r || !r.ok || !r.path) {
    if (r && r.error) flash(t("msg.genericFailed", { err: r.error }));
    return;
  }
  const scan = await API.scanDir({
    path: r.path, recursive: true, maxFiles: DIR_SCAN_MAX, maxDepth: 4,
  });
  if (!scan || !scan.ok) {
    flash(t("msg.scanFailed", { err: (scan && scan.error) || "scan-dir" }));
    return;
  }
  const found = Array.isArray(scan.files) ? scan.files : [];
  if (!found.length) { flash(t("msg.scanEmpty")); return; }
  const take = found.slice(0, room);
  const seen = new Set(state.pathFiles.map(p => p.path));
  let added = 0;
  for (const f of take) {
    if (!f || !f.path || seen.has(f.path)) continue;
    seen.add(f.path);
    state.pathFiles.push({
      path: f.path,
      name: f.name || String(f.path).split(/[\\/]/).pop(),
      size: f.size || 0,
      ext: f.ext || "",
    });
    added++;
  }
  renderFiles();
  flash(t("msg.dirScanned", { n: added }));
  if (scan.truncated || found.length > take.length) {
    flash(t("msg.dirScanTruncated", { n: state.pathFiles.length }));
  }
  if (state.files.length) flash(t("msg.mixedSources"));
}
$("#btn-pick-dir").addEventListener("click", pickDirectory);

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
  if (state.running) return;
  const files = state.files;
  const paths = state.pathFiles.map(p => p.path).filter(Boolean);
  if (!files.length && !paths.length) return;
  // 两条链路语义不同（上传临时文件 vs 直读路径），混用会分不清页序，先要求二选一
  if (files.length && paths.length) { flash(t("msg.mixedSources")); return; }
  if (paths.length > DIR_SCAN_MAX) { flash(t("msg.dirLimitReached", { n: DIR_SCAN_MAX })); return; }
  state.running = true;
  state.batchSource = paths.length ? "paths" : "files";
  renderRunButtons();
  $("#progress").hidden = false;
  setProgress(2, paths.length ? t("prog.reading") : t("prog.uploading"));
  const prompt = $("#prompt-box").value.trim();
  try {
    const result = await API.recognize({
      files, paths, prompt, mode: state.mode,
      name: paths.length ? baseNameOf(state.pathFiles[0]) : "",
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
      // E3：截断/页数不符必须有痕迹（结果区提示条 + 日志）
      logResultWarnings(result);
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

function baseNameOf(p) {
  const name = (p && (p.name || p.path)) || "";
  return String(name).replace(/^.*[\\/]/, "").replace(/\.[^.]+$/, "") || "";
}

function setProgress(pct, status) {
  const p = Math.max(0, Math.min(100, Math.round((pct || 0) * 100)));
  $("#prog-bar").style.width = p + "%";
  $("#prog-pct").textContent = p + "%";
  if (status != null) $("#prog-status").textContent = status;
}

/* ── E3 截断/页数不符（S2）────────────────────────────────
   后端新字段：truncated / pagesMismatch / finishReason / groups / droppedBlocks。
   字段缺失（后端尚未落地）时按「无问题」处理，不能因此报错或阻止流程。 */
function numOrNull(v) {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}
function truncationInfo(r) {
  if (!r || typeof r !== "object") return null;
  const truncated = !!r.truncated;
  const mismatch = !!r.pagesMismatch;
  if (!truncated && !mismatch) return null;
  const expected = numOrNull(r.expectedPages != null ? r.expectedPages : r.expected_pages);
  const actualRaw = numOrNull(r.actualPages != null ? r.actualPages : r.actual_pages);
  const actual = actualRaw != null ? actualRaw
    : (Array.isArray(r.pages) ? r.pages.length : null);
  return {
    truncated,
    mismatch,
    finish: r.finishReason || r.finish_reason || "",
    groups: numOrNull(r.groups),
    dropped: numOrNull(r.droppedBlocks != null ? r.droppedBlocks : r.dropped_blocks),
    expected,
    actual,
  };
}
function resultWarnLines(r) {
  const info = truncationInfo(r);
  if (!info) return [];
  const lines = [];
  if (info.truncated) {
    lines.push(t("warn.truncatedTitle"));
    lines.push(t("warn.truncatedBody", { finish: info.finish || "length" }));
  }
  if (info.mismatch) {
    lines.push(info.expected != null && info.actual != null
      ? t("warn.pagesMismatchBody", { expected: info.expected, actual: info.actual })
      : t("warn.pagesMismatchUnknown"));
  }
  if (info.groups != null && info.groups > 1) lines.push(t("warn.groups", { n: info.groups }));
  if (info.dropped != null && info.dropped > 0) lines.push(t("warn.droppedBlocks", { n: info.dropped }));
  lines.push(t("warn.retryHint"));
  return lines;
}
function batchWarnLines(result) {
  const items = (result && result.items) || [];
  const n = items.filter(it => it && (it.truncated || it.pagesMismatch)).length;
  if (!n) return [];
  return [t("warn.batchTruncated", { n }), t("warn.retryHint")];
}
function renderResultWarn(lines) {
  const box = $("#result-warn");
  const body = $("#result-warn-body");
  const list = (lines || []).filter(Boolean);
  if (!list.length) {
    box.hidden = true;
    body.innerHTML = "";
    return;
  }
  box.hidden = false;
  body.innerHTML = list.map((line, i) =>
    `<div class="rw-line${i === 0 ? " rw-title" : ""}">${escapeHtml(line)}</div>`).join("");
}
function logResultWarnings(result) {
  if (!result) return;
  if (result.mode === "batch") {
    const items = result.items || [];
    const n = items.filter(it => it && (it.truncated || it.pagesMismatch)).length;
    if (n) flash(t("warn.batchTruncated", { n }));
    return;
  }
  const info = truncationInfo(result);
  if (!info) return;
  const bits = [];
  if (info.truncated) bits.push(t("warn.logTruncated", { finish: info.finish || "length" }));
  if (info.mismatch) {
    bits.push(info.expected != null && info.actual != null
      ? t("warn.logPagesMismatch", { expected: info.expected, actual: info.actual })
      : t("warn.pagesMismatchUnknown"));
  }
  flash(bits.join(" · "));
}

/* ── 结果渲染（轻量 Markdown）──────────────────────────── */
// 模型输出里的表格是原生 HTML（<table><tr><td>…），需要渲染而非转义；
// 但它经 sanitizeHtmlFragment 白名单重建后才允许进入 innerHTML（B1）。
const HTML_BLOCK_RE = /^\s*<\/?(table|thead|tbody|tfoot|tr|th|td|ul|ol|li|p|div|br|figure|figcaption|blockquote|pre|code|strong|em|img|a|h[1-6])\b/i;

function mdToHtml(md) {
  const lines = String(md).split(/\r?\n/);
  const out = [];
  let inCode = false, codeBuf = [], listType = null;

  const closeList = () => { if (listType) { out.push(`</${listType}>`); listType = null; } };

  // 行内链接/图片的协议白名单（S5）：只放行 http(s)/mailto、图片 data:image、相对路径
  const INLINE_SCHEME_RE = /^[a-zA-Z][a-zA-Z0-9+.-]*:/;
  const isSafeUrl = (u, allowImage) => {
    const v = String(u == null ? "" : u).trim();
    if (!v) return false;
    if (/^(?:https?:\/\/|mailto:)/i.test(v)) return true;
    if (allowImage && /^data:image\/[a-z0-9.+-]+[;,]?/i.test(v)) return true;
    return !INLINE_SCHEME_RE.test(v);   // 无 scheme ⇒ 相对路径/锚点
  };

  const inline = s => escapeHtml(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*\n]+)\*/g, "<em>$1</em>")
    .replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (m0, alt, src) =>
      isSafeUrl(src, true) ? `<img alt="${alt}" src="${src}">` : "")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (m0, txt, href) =>
      isSafeUrl(href, false)
        ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${txt}</a>`
        : txt);

  for (let li = 0; li < lines.length; li++) {
    const raw = lines[li];
    if (/^```/.test(raw.trim())) {
      if (inCode) { out.push(`<pre><code>${escapeHtml(codeBuf.join("\n"))}</code></pre>`); codeBuf = []; inCode = false; }
      else { closeList(); inCode = true; }
      continue;
    }
    if (inCode) { codeBuf.push(raw); continue; }

    const line = raw.trimEnd();
    let m;
    // 原生 HTML 块：连续块行合并后消毒（多行 <table>/<div> 结构因此不会被拆散）。
    // 续行放宽到「以 <标签 开头」的行，这样 <div> 里嵌套的 <span>/<img> 等
    // 也会交给消毒器处理，而不是被拆成 Markdown 段落。
    if (HTML_BLOCK_RE.test(line)) {
      closeList();
      const buf = [line];
      while (li + 1 < lines.length) {
        const next = lines[li + 1].trimEnd();
        const isCont = /^\s*<\s*[!/]?\s*[a-zA-Z]/.test(next);
        if (!HTML_BLOCK_RE.test(next) && !isCont) break;
        buf.push(next);
        li++;
      }
      const safe = sanitizeHtmlFragment(buf.join("\n"));
      if (safe.trim()) out.push(safe);
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
  $("#batch-actions").hidden = true;
  $("#batch-error").hidden = true;
  $(".md-wrap").hidden = false;
  renderResultWarn(resultWarnLines(result));
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
function firstLine(s) {
  const txt = String(s == null ? "" : s);
  return txt.split(/\r?\n/)[0] || "";
}
function showBatchResult(result) {
  const items = result.items || [];
  $("#result").hidden = false;
  $("#btn-copy").disabled = false;
  $("#btn-save").disabled = false;
  const box = $("#batch-list");
  box.hidden = false;
  box.innerHTML = items.map((it, i) => {
    const ok = !!it.ok;
    const badge = ok ? `<span class="chip chip-ok">${escapeHtml(t("chip.ok"))}</span>`
      : it.skipped ? `<span class="chip chip-muted">${escapeHtml(t("chip.skipped"))}</span>`
      : `<span class="chip chip-live">${escapeHtml(t("chip.fail"))}</span>`;
    // E3：逐文件也标出截断/页数不符
    const extra = (it.truncated ? `<span class="chip chip-warn">${escapeHtml(t("chip.truncated"))}</span>` : "")
      + (it.pagesMismatch ? `<span class="chip chip-warn">${escapeHtml(t("chip.mismatch"))}</span>` : "");
    // 失败原因不截断：列表里元信息只放首行（CSS 省略号收敛），点开看完整错误
    const meta = ok
      ? escapeHtml(t("result.charsTime", { chars: it.chars, time: fmtTime(it.elapsed) }))
      : escapeHtml(firstLine(it.error) || "—");
    return `
      <button class="batch-item${i === state.viewIndex ? " on" : ""}" data-i="${i}" title="${escapeHtml(ok ? "" : (it.error || ""))}">
        <span class="bn" title="${escapeHtml(it.name)}">${escapeHtml(it.name)}</span>
        ${badge}${extra}
        <span class="bm">${meta}</span>
        <span class="bx">${escapeHtml(ok ? t("result.view") : t("result.viewError"))}</span>
      </button>`;
  }).join("");
  $("#result-meta").textContent = t("result.batchMeta", {
    done: result.done ?? items.filter(i => i.ok).length,
    total: items.length,
    time: fmtTime(result.elapsed),
  });
  // 失败项可重试；失败原因在下方详情区完整展示
  $("#batch-actions").hidden = !items.some(it => it && !it.ok);
  $("#batch-error").hidden = true;
  $(".md-wrap").hidden = false;
  renderResultWarn(batchWarnLines(result));
  const okIdx = items.findIndex(it => it.ok);
  state.viewIndex = okIdx >= 0 ? okIdx : 0;
  if (items.length) viewBatchItem(state.viewIndex);
}

function showBatchError(it) {
  const box = $("#batch-error");
  box.hidden = false;
  box.innerHTML = `<div class="be-title">${escapeHtml(t("msg.batchErrorTitle"))} — ${escapeHtml(it.name || "")}</div>`
    + `<div class="be-body">${escapeHtml(it.error || t("chip.fail"))}</div>`;
}
function viewBatchItem(i) {
  const items = state.batchItems || [];
  const it = items[i];
  if (!it) return;
  state.viewIndex = i;
  $$("#batch-list .batch-item").forEach(b => b.classList.toggle("on", +b.dataset.i === i));
  if (!it.ok) {
    // 失败项：展示完整错误，隐藏正文区（避免残留上一次结果）
    $(".md-wrap").hidden = true;
    $("#page-tabs").hidden = true;
    // 没有可复制/保存的正文：禁用按钮，避免复制到上一次的结果
    $("#btn-copy").disabled = true;
    $("#btn-save").disabled = true;
    showBatchError(it);
    return;
  }
  $(".md-wrap").hidden = false;
  $("#batch-error").hidden = true;
  $("#btn-copy").disabled = false;
  $("#btn-save").disabled = false;
  state.lastResult = {
    text: it.text, pages: it.pages || [it.text], outPath: it.outPath, elapsed: it.elapsed,
    truncated: !!it.truncated, pagesMismatch: !!it.pagesMismatch,
    finishReason: it.finishReason || it.finish_reason || "",
  };
  state.curPage = 0;
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
  $("#result-meta").textContent = r.elapsed == null
    ? t("result.historyMeta", { chars: (r.text || "").length })
    : t("result.pageMeta", {
        n: pages.length || 1,
        chars: (r.text || "").length,
        time: fmtTime(r.elapsed),
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

/* ── B8 批量失败项重试（S4）：只重跑失败的文件，成功后替换该项 ── */
function findSourceForItem(name, usedFiles, usedPaths, sourceKind) {
  // 只从本轮批量识别的来源里找：目录识别的失败项不该被同名上传文件顶替
  if (sourceKind !== "paths") {
    for (let i = 0; i < state.files.length; i++) {
      if (!usedFiles.has(i) && state.files[i].name === name) {
        usedFiles.add(i);
        return { kind: "file", file: state.files[i] };
      }
    }
  }
  if (sourceKind !== "files") {
    for (let i = 0; i < state.pathFiles.length; i++) {
      if (!usedPaths.has(i) && state.pathFiles[i].name === name) {
        usedPaths.add(i);
        return { kind: "path", path: state.pathFiles[i].path };
      }
    }
  }
  return null;
}
async function retryFailedItems() {
  if (state.running || !state.batchItems) return;
  const failed = state.batchItems
    .map((it, idx) => ({ it, idx }))
    .filter(x => x.it && !x.it.ok);          // 失败的 + 取消跳过的都算
  if (!failed.length) return;
  const usedFiles = new Set(), usedPaths = new Set();
  const files = [], paths = [], targets = [];
  for (const x of failed) {
    const src = findSourceForItem(x.it.name, usedFiles, usedPaths, state.batchSource);
    if (!src) continue;
    if (src.kind === "file") files.push(src.file); else paths.push(src.path);
    targets.push(x.idx);
  }
  if (!targets.length) { flash(t("msg.retryNoSource")); return; }
  state.running = true;
  renderRunButtons();
  $("#btn-retry-failed").disabled = true;
  $("#progress").hidden = false;
  setProgress(2, t("msg.retrying", { n: targets.length }));
  try {
    const result = await API.recognize({
      files, paths, prompt: $("#prompt-box").value.trim(), mode: "batch",
    });
    const newItems = (result && result.items) || [];
    let okN = 0;
    targets.forEach((idx, k) => {
      const ni = newItems[k];
      if (!ni) return;
      state.batchItems[idx] = ni;
      if (ni.ok) okN++;
    });
    const agg = state.lastBatchResult || { items: state.batchItems };
    agg.items = state.batchItems;
    agg.done = state.batchItems.filter(i => i.ok).length;
    agg.elapsed = (agg.elapsed || 0) + ((result && result.elapsed) || 0);
    state.lastBatchResult = agg;
    flash(t("msg.retryDone", { ok: okN, n: targets.length }));
    showBatchResult(agg);
    logResultWarnings(agg);
  } catch (e) {
    flash(t("msg.genericFailed", { err: e.message || e }));
  } finally {
    state.running = false;
    renderRunButtons();
    $("#btn-retry-failed").disabled = false;
    setTimeout(() => { if (!state.running) $("#progress").hidden = true; }, 1500);
    refreshHistory();
  }
}
$("#btn-retry-failed").addEventListener("click", retryFailedItems);

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
  // bridge 失败时返回 {items:[], error}，这里必须兜底（旧实现直接解构会 TypeError）
  const r = await API.listHistory();
  const items = (r && r.items) || [];
  const head = $("#history-head"), list = $("#history-list");
  if (!items.length) { head.hidden = true; list.hidden = true; return; }
  head.hidden = false; list.hidden = false;
  list.innerHTML = items.map(it => {
    const when = fmtDateTime(it.mtime);
    return `
    <button class="history-item" data-name="${escapeHtml(it.name)}">
      <span class="hn">${escapeHtml(it.name)}</span>
      <span class="hm"${when ? ` title="${escapeHtml(when)}"` : ""}>${escapeHtml(fmtSize(it.size))}${when ? " · " + escapeHtml(when) : ""}</span>
      <span class="hx" data-del="${escapeHtml(it.name)}" role="button" aria-label="${escapeHtml(t("history.delete"))}" tabindex="0">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg>
      </span>
    </button>`;
  }).join("");
}
$("#history-list").addEventListener("click", async e => {
  const del = e.target.closest(".hx");
  if (del) {
    e.stopPropagation();
    const name = del.dataset.del;
    if (!confirm(t("msg.confirmDelete", { name }))) return;
    const r = await API.deleteHistory(name);
    if (r && !r.ok) flash(t("msg.genericFailed", { err: r.error || "history-delete" }));
    refreshHistory();
    return;
  }
  const item = e.target.closest(".history-item");
  if (!item) return;
  const r = await API.readHistory(item.dataset.name);
  if (r && r.ok) {
    // 历史文件没有「页/耗时」概念：elapsed=null，元信息显示字数 + 历史文件
    // 同时清掉批量态，否则语言切换/重渲染会把旧批量列表又画回来
    state.batchItems = null;
    state.lastBatchResult = null;
    state.lastResult = { text: r.text, pages: [r.text], outPath: r.name, elapsed: null };
    state.curPage = 0;
    showResult(state.lastResult);
    flash(t("msg.loadedHistory", { name: r.name }));
  } else {
    flash(t("msg.genericFailed", { err: (r && r.error) || "history-read" }));
  }
});

/* ── 模型页 ───────────────────────────────────────────── */
function renderModelPage() {
  const st = state.status;
  if (!st) return;

  // 运行环境自检
  // PyTorch 行与组件卡片同口径：显示的是引擎实际将加载的 torch；来源用
  // envInstalled（组件标记）区分——venv 环境与组件钉定版本不一致时标黄并
  // 给出口径说明，避免「推理依赖 2.11 / 组件卡 2.8」各说各话（用户反馈实录）。
  const torchOk = !!st.torch;
  const tfOk = !!st.transformers;
  const fromComponent = !!(st.envInstalled && st.torch);
  const pin = st.torchPin || (st.envInstalled || {}).torch || "";
  const torchMismatch = !!st.torch && !!pin && !String(st.torch).startsWith(pin);
  const torchDetail = st.torch
    ? (st.torch + (fromComponent ? "" : " · venv")
       + (torchMismatch ? ` — ${t("env.torchMismatch", { pin })}` : ""))
    : t("env.notInstalled");
  const cells = [
    {
      title: t("env.deps"),
      items: [
        { label: "PyTorch", ok: torchOk && !torchMismatch, detail: torchDetail },
        { label: "Transformers", ok: tfOk, detail: st.transformers || t("env.notInstalled") },
      ],
    },
    {
      title: t("env.device"),
      items: [
        { label: "CPU", ok: true, detail: t("env.available") },
        { label: "CUDA GPU", ok: st.cuda, detail: st.cuda ? `${st.cudaName} · ${st.vramGb} GB`
          : (st.cudaBuild ? t("env.noCuda") : t("env.noCudaCpuBuild")) },
        { label: t("env.interpreter"), ok: true, detail: shortPath(st.interpreter || "—") },
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
  // 下载/加载中给出取消出口（后端 cancel() 对「下载/识别」两类任务都生效）
  const cancelable = ["downloading", "loading"].includes(st.modelState);
  const cancelBtn = $("#btn-model-cancel");
  cancelBtn.hidden = !cancelable;
  cancelBtn.disabled = !cancelable;

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
  $("#btn-model-cancel").hidden = false;
  $("#btn-model-cancel").disabled = false;
  try {
    const r = await API.startDownload();
    if (!r || !r.ok) {
      flash((r && r.error) || t("msg.cannotDownload"));
      $("#btn-model-cancel").hidden = true;
    }
  } catch (e) {
    flash(t("msg.genericFailed", { err: e.message || e }));
    $("#btn-model-cancel").hidden = true;
  }
});
$("#btn-model-load").addEventListener("click", async () => {
  $("#model-progress").hidden = false;
  setModelProgress(5, t("model.startLoad"));
  $("#btn-model-cancel").hidden = false;
  $("#btn-model-cancel").disabled = false;
  try {
    const r = await API.startLoad();
    if (!r || !r.ok) {
      flash((r && r.error) || t("msg.cannotLoad"));
      $("#model-progress").hidden = true;
      $("#btn-model-cancel").hidden = true;
    }
  } catch (e) {
    flash(t("msg.genericFailed", { err: e.message || e }));
    $("#model-progress").hidden = true;
    $("#btn-model-cancel").hidden = true;
  }
});
// 取消模型下载/加载：复用既有 /api/cancel（B5）
$("#btn-model-cancel").addEventListener("click", async () => {
  const ok = await API.cancel();
  flash(ok ? t("msg.cancelRequested") : t("msg.genericFailed", { err: "cancel" }));
});

function setModelProgress(pct, status) {
  const p = Math.max(0, Math.min(100, Math.round((pct || 0) * 100)));
  $("#model-prog-bar").style.width = p + "%";
  $("#model-prog-pct").textContent = p + "%";
  if (status != null) $("#model-prog-status").textContent = status;
}

/* ── 推理引擎组件卡片（torch 运行时，按硬件选装）───────────── */
async function loadEnv() {
  try { state.env = await API.getEnv(); } catch { state.env = null; }
  renderFlavorCard();
}

function renderFlavorCard() {
  const env = state.env, list = $("#flavor-list");
  if (!env || !env.ok) {
    list.innerHTML = `<div class="d" style="opacity:.6;padding:6px 0">${escapeHtml(env?.error || t("env.checking"))}</div>`;
    $("#flavor-chip").textContent = t("env.checking");
    $("#btn-flavor-download").disabled = true;
    return;
  }
  const pick = state.envFlavor || env.selected || env.recommended;
  const installedFlavor = env.installed?.flavor;

  list.innerHTML = env.options.map(o => {
    const active = o.key === pick;
    const badges = [
      o.recommended ? `<span class="chip chip-accent">${escapeHtml(t("env.recommended"))}</span>` : "",
      o.installed ? `<span class="chip chip-ok">${escapeHtml(t("env.installed"))}</span>` : "",
      !o.suitable ? `<span class="chip chip-muted">${escapeHtml(t("env.unsuitable"))}</span>` : "",
    ].join("");
    return `
    <label class="mc-meta" style="display:flex;align-items:flex-start;gap:10px;padding:10px;border:1px solid var(--line);border-radius:10px;margin-top:8px;cursor:pointer;${active ? "border-color:var(--accent);background:var(--accent-soft,rgba(0,0,0,.03))" : ""}" data-flavor="${o.key}">
      <input type="radio" name="flavor" value="${o.key}" ${active ? "checked" : ""} ${env.envDownloading ? "disabled" : ""} style="margin-top:3px">
      <span style="flex:1">
        <span style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <b>${escapeHtml(o.label)}</b>
          <span style="opacity:.6">${o.size_mb >= 1024 ? (o.size_mb / 1024).toFixed(1) + " GB" : o.size_mb + " MB"}</span>
          ${badges}
        </span>
        <span style="display:block;opacity:.75;margin-top:2px">${escapeHtml(o.desc)}</span>
        ${o.note ? `<span style="display:block;color:#b45309;margin-top:2px">${escapeHtml(o.note)}</span>` : ""}
      </span>
    </label>`;
  }).join("");

  // 点选即记住（持久化在后端 set_env_flavor；这里不阻塞渲染）
  list.querySelectorAll("input[name=flavor]").forEach(r => r.addEventListener("change", async () => {
    state.envFlavor = r.value;
    renderFlavorCard();
    try { await API.setEnvFlavor(r.value); } catch {}
  }));

  // 状态 chip 与按钮
  const chip = $("#flavor-chip");
  const inst = env.installed;
  if (env.envDownloading) {
    chip.className = "chip chip-warn";
    chip.textContent = t("task.envDl");
  } else if (inst) {
    chip.className = "chip chip-ok";
    chip.textContent = t("env.installedChip", { flavor: inst.flavor, ver: inst.torch || "" });
  } else {
    chip.className = "chip chip-warn";
    chip.textContent = t("env.notInstalled");
  }
  $("#btn-flavor-download").disabled = !!env.envDownloading || !pick || !env.options.find(o => o.key === pick)?.suitable;
  $("#btn-flavor-cancel").hidden = !env.envDownloading;
  $("#flavor-progress").hidden = !env.envDownloading;
}

$("#btn-flavor-download").addEventListener("click", async () => {
  const env = state.env;
  const pick = state.envFlavor || env?.selected || env?.recommended;
  if (!pick) { flash(t("env.pickFirst")); return; }
  $("#flavor-progress").hidden = false;
  setFlavorProgress(0, t("model.prepDownload"));
  try {
    const r = await API.startEnvDownload(pick);
    if (!r || !r.ok) {
      flash((r && r.error) || t("msg.cannotLoad"));
      $("#flavor-progress").hidden = true;
      return;
    }
    if (r.already) { $("#flavor-progress").hidden = true; loadEnv(); }
  } catch (e) {
    flash(t("msg.genericFailed", { err: e.message || e }));
    $("#flavor-progress").hidden = true;
  }
});
$("#btn-flavor-cancel").addEventListener("click", () => API.cancelEnvDownload());

function setFlavorProgress(pct, status) {
  const p = Math.max(0, Math.min(100, Math.round((pct || 0) * 100)));
  $("#flavor-bar").style.width = p + "%";
  if (status != null) $("#flavor-status").textContent = p + "% · " + status;
}
API.on("env-progress", ({ pct, status }) => {
  $("#flavor-progress").hidden = false;
  setFlavorProgress(pct, status);
  if (pct >= 1 || pct === 0) {   // 完成／失败/取消 → 刷新安装态
    setTimeout(loadEnv, 800);
    API.getStatus().then(s => { state.status = s; renderModelPage(); }).catch(() => {});
  }
});

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
// 缩放下拉：后端可能存着下拉里没有的档位（旧版本/手改 settings.json），
// 不能让它显示成空白——缺档就补一个 option 再选中。
function setScaleSelect(v) {
  const sel = $("#set-scale");
  const val = String(v || 100);
  if (![...sel.options].some(o => o.value === val)) {
    const o = document.createElement("option");
    o.value = val;
    o.textContent = val + "%";
    sel.appendChild(o);
  }
  sel.value = val;
}
/* ── 识别参数夹取（S1）：前端先夹到合法区间再提交，返回值回显到输入框 ──
   maxPageTokens: 0 = 不限，有效 256..65536
   maxMergedPages: 0 = 不限，有效 0..200 */
const SETTINGS_LIMITS = {
  maxPageTokens: { min: 256, max: 65536, def: 4096, allowZero: true },
  maxMergedPages: { min: 0, max: 200, def: 20, allowZero: true },
};
function clampSettingValue(key, raw) {
  const spec = SETTINGS_LIMITS[key] || { min: 0, max: Number.MAX_SAFE_INTEGER, def: 0, allowZero: true };
  let v = parseInt(String(raw == null ? "" : raw).trim(), 10);
  if (!Number.isFinite(v)) v = spec.def;
  if (v < 0) v = spec.allowZero ? 0 : spec.min;
  if (v > 0 && v < spec.min) v = spec.min;
  if (v > spec.max) v = spec.max;
  return v;
}
function setIntSetting(s, key, sel) {
  const el = $(sel);
  const spec = SETTINGS_LIMITS[key];
  if (Object.prototype.hasOwnProperty.call(s, key) && s[key] != null) {
    el.value = String(clampSettingValue(key, s[key]));
  } else if (!el.value) {
    // 后端尚未返回该键时保留用户输入，只有空值才补默认值
    el.value = String(spec.def);
  }
}
function fillSettings(s) {
  // 后端校验失败时返回 {ok:false,error}：不能当成设置对象把界面清空
  if (!s || s.ok === false) return;
  state.settings = s;
  $("#set-model-dir").value = s.modelDir || "";
  $("#set-mirror").value = s.mirror || "";
  setScaleSelect(s.scale || 100);
  $$("#set-device button").forEach(b => b.classList.toggle("on", b.dataset.v === (s.devicePref || "auto")));
  $$("#set-theme button").forEach(b => b.classList.toggle("on", b.dataset.v === (s.theme || "light")));
  $$("#set-outfmt button").forEach(b => b.classList.toggle("on", b.dataset.v === (s.outFormat || "md")));
  renderLangOptions(s.languages);
  $("#set-lang").value = s.lang || "zh";
  // S1 识别参数：键缺失（后端未落地）时保持现有输入，不弹回默认
  setIntSetting(s, "maxPageTokens", "#set-max-tokens");
  setIntSetting(s, "maxMergedPages", "#set-max-merged-pages");
  const crop = typeof s.cropMode === "string" ? s.cropMode : "auto";
  $$("#set-crop-mode button").forEach(b => b.classList.toggle("on", b.dataset.v === crop));
  if (typeof s.dropHeaders === "boolean") $("#set-drop-headers").checked = s.dropHeaders;
  renderPythonSettings(s);
  applyTheme(s.theme || "light");
  applyScale(s.scale || 100);
  // 语言变化：加载对应语言包并重渲染（后端文案由 set_settings 同步）
  const wantLang = s.lang || "zh";
  if (window.I18N.lang !== wantLang) window.I18N.setLang(wantLang);
}
let saveTimer = null;
let pendingSettings = {};   // 同一 debounce 窗口内的多次改动必须合并，不能只发最后一次
function pushSettings(patch) {
  Object.assign(pendingSettings, patch || {});
  clearTimeout(saveTimer);
  saveTimer = setTimeout(flushSettings, 250);
}
async function flushSettings() {
  const patch = pendingSettings;
  pendingSettings = {};
  if (!Object.keys(patch).length) return;
  try {
    const s = await API.setSettings(patch);
    if (!s || s.ok === false) {
      flash((s && s.error) || t("msg.genericFailed", { err: "settings" }));
      return;
    }
    fillSettings(s);
    const st = await API.getStatus();
    applyStatus(st);
    flash(t("msg.settingsSaved"));
  } catch (e) {
    flash(t("msg.genericFailed", { err: e.message || e }));
  }
}
$("#set-model-dir").addEventListener("change", () => pushSettings({ modelDir: $("#set-model-dir").value.trim() }));
$("#btn-model-dir-browse").addEventListener("click", async () => {
  const r = await API.browseFolder($("#set-model-dir").value);
  if (r && r.ok && r.path) { $("#set-model-dir").value = r.path; pushSettings({ modelDir: r.path }); }
  else if (r && r.error) flash(t("msg.genericFailed", { err: r.error }));
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

/* S1 识别参数：数值先夹取再提交（输入框同步回显夹取结果） */
function bindIntSetting(sel, key) {
  $(sel).addEventListener("change", () => {
    const el = $(sel);
    const v = clampSettingValue(key, el.value);
    el.value = String(v);
    pushSettings({ [key]: v });
  });
}
bindIntSetting("#set-max-tokens", "maxPageTokens");
bindIntSetting("#set-max-merged-pages", "maxMergedPages");
$("#set-crop-mode").addEventListener("click", e => {
  const b = e.target.closest("button");
  if (!b) return;
  $$("#set-crop-mode button").forEach(x => x.classList.toggle("on", x === b));
  pushSettings({ cropMode: b.dataset.v });
});
$("#set-drop-headers").addEventListener("change", () => {
  pushSettings({ dropHeaders: $("#set-drop-headers").checked });
});

/* ── 设置页：Python 解释器（issue #2）──────────────────── */
const pyUI = {
  checkedPath: null,     // null=未检测；否则为已通过检测的路径（""=恢复默认）
  checking: false,
  renderedPath: null,    // 已渲染进输入框的 pythonPath（避免任意设置保存清掉编辑）
  lastProbe: null,       // 最近一次探测结果（语言切换时重渲染用）
};
function renderPythonSettings(s) {
  // 冻结版（PyInstaller）无解释器概念：整卡隐藏
  $("#py-card").hidden = !!s.frozen;
  if (s.frozen) return;
  $("#py-current").textContent = s.interpreter || "";
  $("#py-current").title = s.interpreter || "";
  const saved = s.pythonPath || "";
  if (pyUI.renderedPath === null || pyUI.renderedPath !== saved) {
    pyUI.renderedPath = saved;
    $("#set-python-path").value = saved;
    $("#btn-python-reset").hidden = !saved.trim();
    pyUI.checkedPath = null;
    $("#btn-python-apply").disabled = true;
    $("#py-probe-row").hidden = true;
    $("#py-probe-out").textContent = "";
  }
}
function renderProbeResult(r) {
  pyUI.lastProbe = r;          // 语言切换后 retranslate 需要用它重渲染
  const out = $("#py-probe-out");
  $("#py-probe-row").hidden = false;
  if (!r.ok) {
    out.innerHTML = `<span style="color:#b45309">${escapeHtml(r.error || "probe failed")}</span>`;
    return;
  }
  const rows = [
    ["Python", r.version || "—"],
    ["PyTorch", r.torch || t("env.notInstalled")],
    ["CUDA", r.torch ? (r.cuda
      ? `${t("env.available")} · ${r.cudaName || ""} · ${r.vramGb || 0} GB`
      : (r.cudaBuild ? t("py.cudaBuildNoGpu", { build: r.cudaBuild }) : t("env.noCuda")))
      : "—"],
    ["Transformers", r.transformers || t("env.notInstalled")],
    ["pywebview", r.pywebview || t("env.notInstalled")],
  ];
  out.innerHTML = rows.map(([k, v]) =>
    `<div><span style="opacity:.6">${escapeHtml(k)}:</span> ${escapeHtml(String(v))}</div>`).join("") +
    (r.depsOk
      ? `<div style="color:#15803d;margin-top:4px">${escapeHtml(t("py.depsOkHint"))}</div>`
      : `<div style="color:#b45309;margin-top:4px">${escapeHtml(t("py.depsMissing"))}</div>`);
}
$("#btn-python-browse").addEventListener("click", async () => {
  const r = await API.browseFile($("#set-python-path").value);
  if (r && r.ok && r.path) {
    $("#set-python-path").value = r.path;
    pyUI.checkedPath = null;          // 路径变了，旧检测结果作废
    $("#btn-python-apply").disabled = true;
  } else if (r && r.error) {
    flash(t("msg.genericFailed", { err: r.error }));
  }
});
// 手动改路径 → 已通过的检测结果作废，须重新「检测」
$("#set-python-path").addEventListener("input", () => {
  pyUI.checkedPath = null;
  $("#btn-python-apply").disabled = true;
});
$("#btn-python-check").addEventListener("click", async () => {
  if (pyUI.checking) return;
  pyUI.checking = true;
  pyUI.checkedPath = null;
  $("#btn-python-check").disabled = true;
  $("#btn-python-apply").disabled = true;
  $("#py-probe-row").hidden = false;
  $("#py-probe-out").textContent = t("py.checking");
  try {
    const r = await API.probePython($("#set-python-path").value.trim());
    if (r.ok && r.depsOk) {
      pyUI.checkedPath = r.path || $("#set-python-path").value.trim();
      $("#btn-python-apply").disabled = false;
    }
    renderProbeResult(r);
  } finally {
    pyUI.checking = false;
    $("#btn-python-check").disabled = false;
  }
});
$("#btn-python-apply").addEventListener("click", async () => {
  if (pyUI.checkedPath === null) return;
  const path = pyUI.checkedPath;
  if (!confirm(t("py.confirmApply", { path: path || t("py.defaultEnv") }))) return;
  const r = await API.applyPython(path);
  if (r.ok && r.restarting) {
    $("#btn-python-apply").disabled = true;
    flash(t("py.restarting"));
  } else {
    flash(r.error || t("msg.failed", { err: "apply-python" }));
  }
});
$("#btn-python-reset").addEventListener("click", async () => {
  // 恢复默认 = 清空 pythonPath 并重启回启动器选定的 venv
  if (!confirm(t("py.confirmApply", { path: t("py.defaultEnv") }))) return;
  const r = await API.applyPython("");
  if (r.ok && r.restarting) flash(t("py.restarting"));
  else flash(r.error || t("msg.failed", { err: "apply-python" }));
});

/* ── SSE 事件 ─────────────────────────────────────────── */
API.on("status", s => applyStatus(s));
API.on("progress", ({ pct, status }) => {
  $("#progress").hidden = false;
  setProgress(pct, status);
});
API.on("dl-progress", ({ pct, status }) => {
  $("#model-progress").hidden = false;
  setModelProgress(pct, status);
  // pct 0 = 取消/失败，1 = 完成：两种终点都收起取消按钮
  if (pct === 0 || pct >= 1) $("#btn-model-cancel").hidden = true;
});
API.on("load-progress", ({ pct, status }) => {
  $("#model-progress").hidden = false;
  setModelProgress(pct, status);
  if (pct === 0 || pct >= 1) $("#btn-model-cancel").hidden = true;
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
  // 传输层回退到 mock（file:// 预览或后端不可达）时不能静默：常驻横幅说明
  if (API.mode !== "web") {
    const banner = $("#mode-banner");
    if (banner) banner.hidden = false;
  }
  try {
    const s = await API.getSettings();
    await window.I18N.setLang(s.lang || "zh");   // 先定语言再刷界面
    fillSettings(s);
    applyStatus(await API.getStatus());
  } catch (e) {
    console.warn("init status failed", e);
    // 后端在但读不到设置/状态：同样给出可见提示，不让界面停在空白
    const banner = $("#mode-banner");
    if (banner) banner.hidden = false;
    flash(t("msg.genericFailed", { err: e.message || e }));
  }
  refreshHistory();
  flash(API.mode === "web" ? t("msg.readyWeb") : t("msg.readyDemo"));
})();
