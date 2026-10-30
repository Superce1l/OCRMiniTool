/* ============================================================
   i18n.js — 前端多语言加载器（须在 bridge.js / app.js 之前引入）
   语言文件 webview/locales/{lang}.json 与后端 i18n.py 共用同一份，
   key 为扁平命名，占位符 {param} 用参数替换。
   静态文案经 data-i18n* 属性批量应用：
     data-i18n            → textContent
     data-i18n-html       → innerHTML（仅限可信文案，如 drop.big 的 <b>）
     data-i18n-title      → title 属性
     data-i18n-placeholder→ placeholder 属性
     data-i18n-aria       → aria-label 属性
   动态文案用 I18N.t(key, params)。语言切换后经 onChange 通知 app.js
   重渲染动态区域。
   ============================================================ */
window.I18N = (() => {
  "use strict";

  const FALLBACK = "zh";   // 默认界面语言：中文
  // 兜底语言列表（后端不在时用；正常路径由 /api/settings 的 languages
  // 动态填充——后端扫 webview/locales/*.json，新语言丢一份 JSON 即接入）
  const LANGUAGES = [
    { code: "zh", name: "中文" },
    { code: "en", name: "English" },
    { code: "es", name: "Español" },
  ];
  let dict = {};
  let lang = FALLBACK;
  const changeCbs = new Set();

  function t(key, params) {
    let s = dict[key];
    if (s == null) s = key;
    if (params) {
      s = String(s).replace(/\{(\w+)\}/g, (m, k) =>
        params[k] != null ? String(params[k]) : m);
    }
    return s;
  }
  function has(key) { return dict[key] != null; }

  function applyAll() {
    document.querySelectorAll("[data-i18n]").forEach(el => { el.textContent = t(el.dataset.i18n); });
    document.querySelectorAll("[data-i18n-html]").forEach(el => { el.innerHTML = t(el.dataset.i18nHtml); });
    document.querySelectorAll("[data-i18n-title]").forEach(el => { el.title = t(el.dataset.i18nTitle); });
    document.querySelectorAll("[data-i18n-placeholder]").forEach(el => { el.placeholder = t(el.dataset.i18nPlaceholder); });
    document.querySelectorAll("[data-i18n-aria]").forEach(el => { el.setAttribute("aria-label", t(el.dataset.i18nAria)); });
  }

  async function setLang(next) {
    lang = next || FALLBACK;
    try {
      const r = await fetch(`locales/${lang}.json`);
      dict = r.ok ? await r.json() : {};
      if (typeof dict !== "object" || !dict) dict = {};
    } catch {
      dict = {};   // file:// 预览等场景取不到语言文件：退回 HTML 静态文案
    }
    document.documentElement.lang = lang === "zh" ? "zh-Hans" : lang;
    document.title = t("app.name");
    applyAll();
    changeCbs.forEach(fn => { try { fn(lang); } catch (e) { console.error(e); } });
    return lang;
  }

  return {
    t, has, setLang, applyAll, LANGUAGES,
    localeTag: () => (lang === "zh" ? "zh-Hans" : lang),   // toLocaleTimeString 用
    get lang() { return lang; },
    onChange(fn) { changeCbs.add(fn); return () => changeCbs.delete(fn); },
  };
})();

// 默认语言中文：在设置加载前先按默认语言渲染静态文案，减少闪烁
const boot = () => window.I18N.setLang("zh");
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot, { once: true });
} else {
  boot();
}
