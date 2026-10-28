/* ============================================================
   bridge.js — 统一前端桥接层 window.OCRApi
   与 STTMiniTool 的 bridge.js 同构：
     • web  : 本机 HTTP（桌面 = 本机 server + WebView2/Edge）
              方法打 /api/*，进度经 SSE /api/events 推回
     • mock : 纯展示（file:// 或无后端，回范例数据供设计预览）
   ============================================================ */
(function () {
  "use strict";

  // ── event bus ───────────────────────────────────────────
  const listeners = {};
  function on(ev, fn) { (listeners[ev] ||= new Set()).add(fn); return () => off(ev, fn); }
  function off(ev, fn) { listeners[ev]?.delete(fn); }
  function emit(ev, payload) { listeners[ev]?.forEach(fn => { try { fn(payload); } catch (e) { console.error(e); } }); }

  let MODE = "mock";
  const isHttp = () => location.protocol === "http:" || location.protocol === "https:";

  async function probeHttp() {
    try {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), 1500);
      const r = await fetch("/health", { signal: ctrl.signal });
      clearTimeout(t);
      return r.ok;
    } catch { return false; }
  }

  const ready = new Promise(resolve => {
    async function settle() {
      MODE = (isHttp() && await probeHttp()) ? "web" : "mock";
      console.info("[OCRApi] transport =", MODE);
      if (MODE === "web") connectSSE();
      resolve(MODE);
    }
    if (document.readyState === "complete") setTimeout(settle, 0);
    else window.addEventListener("load", () => setTimeout(settle, 0), { once: true });
  });

  function connectSSE() {
    try {
      const es = new EventSource("/api/events");
      es.onmessage = e => {
        try { const m = JSON.parse(e.data); if (m && m.event) emit(m.event, m.payload); }
        catch { /* 心跳/注释行 */ }
      };
      es.onerror = () => { /* EventSource 自动重连 */ };
    } catch (e) { console.warn("[OCRApi] SSE 无法建立", e); }
  }

  async function apiGet(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }
  async function apiPost(path, body) {
    const r = await fetch(path, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!r.ok) {
      let msg = "HTTP " + r.status;
      try { msg = (await r.json()).error?.message || msg; } catch {}
      throw new Error(msg);
    }
    return r.json();
  }

  const api = {
    on, off, _emit: emit,
    get mode() { return MODE; },
    ready,

    async getStatus() {
      if (MODE === "web") return apiGet("/api/status");
      return MOCK.status;
    },
    async getSettings() {
      if (MODE === "web") return apiGet("/api/settings");
      return MOCK.settings;
    },
    async setSettings(patch) {
      if (MODE === "web") return apiPost("/api/settings", patch);
      Object.assign(MOCK.settings, patch); return MOCK.settings;
    },
    async browseFolder(start) {
      if (MODE === "web") { try { return await apiPost("/api/browse-folder", { start }); } catch {} }
      return { ok: false, path: "" };
    },
    async openOutputDir() {
      if (MODE === "web") { try { return await apiPost("/api/open-output", {}); } catch {} }
      return false;
    },
    async startDownload() {
      if (MODE === "web") return apiPost("/api/download", {});
      mockDownload(); return { ok: true };
    },
    async startLoad() {
      if (MODE === "web") return apiPost("/api/load", {});
      setTimeout(() => { emit("load-progress", { pct: 1, status: "模型就绪" }); emit("status", { modelState: "ready" }); }, 1200);
      return { ok: true };
    },
    async cancel() {
      if (MODE === "web") { try { await apiPost("/api/cancel", {}); } catch {} return true; }
      MOCK.cancelled = true; return true;
    },
    async listHistory() {
      if (MODE === "web") { try { return await apiGet("/api/history"); } catch {} }
      return { items: [] };
    },
    async readHistory(name) {
      if (MODE === "web") { try { return await apiPost("/api/history-read", { name }); } catch (e) { return { ok: false, error: String(e) }; } }
      return { ok: false };
    },
    async deleteHistory(name) {
      if (MODE === "web") { try { return await apiPost("/api/history-delete", { name }); } catch (e) { return { ok: false, error: String(e) }; } }
      return { ok: false };
    },
    // opts: {files: [File], prompt, mode: "merged"|"batch"}
    async recognize(opts) {
      if (MODE === "web") return webRecognize(opts);
      return mockRecognize(opts);
    },
    async shutdown() {
      if (MODE === "web") { try { await apiPost("/api/shutdown", {}); } catch {} }
      return true;
    },
  };

  async function webRecognize(opts) {
    if (!opts.files || !opts.files.length) throw new Error("请先选择图片");
    const fd = new FormData();
    for (const f of opts.files) fd.append("files", f, f.name);
    if (opts.prompt) fd.append("prompt", opts.prompt);
    if (opts.mode) fd.append("mode", opts.mode);
    emit("progress", { pct: 2, status: "上传中…" });
    const r = await fetch("/api/recognize", { method: "POST", body: fd });
    if (!r.ok) {
      let msg = "HTTP " + r.status;
      try { msg = (await r.json()).error?.message || msg; } catch {}
      throw new Error(msg);
    }
    return r.json();   // {mode, text, pages?, items?, outPath, elapsed, cancelled}
  }

  // ════════════════════════════════════════════════════════
  // mock：纯展示（file:// 直接打开 index.html 时的设计预览）
  // ════════════════════════════════════════════════════════
  const MOCK = {
    cancelled: false,
    status: {
      appName: "OCR 小工具", version: "1.0.0", modelState: "ready",
      modelDir: "D:/OCRMiniTool/models/Unlimited-OCR-2", outDir: "D:/OCRMiniTool/outputs",
      modelError: null, loadedDevice: "cpu", devicePref: "auto", mirror: "",
      torch: "2.13.0+cpu", transformers: "4.52.1", cuda: false,
      cudaName: "", vramGb: 0, busy: false, busyLabel: "",
    },
    settings: { modelDir: "", mirror: "", devicePref: "auto", theme: "light", scale: 100 },
  };
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  async function mockRecognize(opts) {
    MOCK.cancelled = false;
    const batch = opts?.mode === "batch";
    const steps = [
      [8, "上传完成"],
      [30, batch ? "批量识别 第 1/2 个：示例.png，已用时 5 秒"
                 : "识别中（gundam 模式），已用时 5 秒（CPU 推理较慢属正常）"],
      [62, batch ? "批量识别 第 2/2 个：示例2.png，已用时 14 秒"
                 : "识别中（gundam 模式），已用时 14 秒（CPU 推理较慢属正常）"],
      [94, "识别完成"], [100, "完成"],
    ];
    for (const [pct, status] of steps) {
      if (MOCK.cancelled) throw new Error("已取消");
      await sleep(500);
      emit("progress", { pct, status });
    }
    if (batch) {
      return {
        mode: "batch", cancelled: false, total: 2, done: 2, elapsed: 18.2,
        text: "示例结果",
        items: [
          { name: "示例.png", ok: true, text: "## 示例标题\n\n这是**展示模式**的批量结果。", chars: 20, elapsed: 8.4 },
          { name: "示例2.png", ok: true, text: "第二张图的识别结果。", chars: 10, elapsed: 9.1 },
        ],
      };
    }
    return {
      mode: "merged",
      text: "# 示例识别结果\n\n这是**展示模式**的示例输出。",
      pages: ["示例识别结果"],
      outPath: "outputs/example.md", elapsed: 21.4, cancelled: false,
    };
  }
  async function mockDownload() {
    for (let p = 0; p <= 100; p += 4) {
      await sleep(160);
      emit("dl-progress", { pct: p / 100, status: `下载 model-00001-of-00002.safetensors…  ${(p * 66).toFixed(0)} / 6600 MB` });
    }
    emit("status", { modelState: "downloaded" });
  }

  window.OCRApi = api;
})();
