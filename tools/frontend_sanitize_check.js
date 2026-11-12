#!/usr/bin/env node
/* ============================================================
   tools/frontend_sanitize_check.js — 结果区 HTML 消毒回归（B1）
   从 webview/js/app.js 抽出纯函数 escapeHtml / sanitizeHtmlFragment / mdToHtml，
   在 Node 里跑「攻击 payload 不得原样进入 innerHTML」+「正常 Markdown/HTML
   渲染不回归」两组断言。退出码 0 = 全过，1 = 有失败。

   用法：
     node tools/frontend_sanitize_check.js        # 汇总
     node tools/frontend_sanitize_check.js -v     # 逐条输出

   说明：这里不引入任何第三方依赖（项目零依赖约束），也刻意不模拟浏览器
   DOM——被检查的消毒器本身就是纯字符串重建，可以直接在 Node 里执行。
   ============================================================ */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const APP_JS = path.join(ROOT, "webview", "js", "app.js");
const VERBOSE = process.argv.includes("-v");

function grab(src, startMarker, endMarker) {
  const i = src.indexOf(startMarker);
  if (i < 0) throw new Error(`app.js 中找不到标记：${startMarker}`);
  const j = src.indexOf(endMarker, i);
  if (j < 0) throw new Error(`app.js 中找不到标记：${endMarker}`);
  return src.slice(i, j);
}

function loadHarness() {
  const src = fs.readFileSync(APP_JS, "utf8");
  const code =
    grab(src, "function escapeHtml(s)", "function flash(msg)") +
    "\n" +
    grab(src, "const HTML_BLOCK_RE", "function showResult(result)");
  const ctx = {
    console,
    String, Set, Object, RegExp, JSON, Math, Number, parseInt,
    document: {}, window: {},
  };
  vm.createContext(ctx);
  vm.runInContext(
    code + "\nthis.mdToHtml = mdToHtml; this.sanitizeHtmlFragment = sanitizeHtmlFragment;",
    ctx,
    { filename: APP_JS },
  );
  return ctx;
}

// 原始标签 / 事件属性 / 危险协议（只检查组出的 HTML，不检查转义后的可见文本）
const FORBIDDEN = [
  [/<script\b/i, "原始 <script 标签"],
  [/<iframe\b/i, "原始 <iframe 标签"],
  [/<link\b/i, "原始 <link 标签"],
  [/<svg\b/i, "原始 <svg 标签"],
  [/<object\b/i, "原始 <object 标签"],
  [/<embed\b/i, "原始 <embed 标签"],
  [/<style\b/i, "原始 <style 标签"],
  [/<meta\b/i, "原始 <meta 标签"],
  [/<[a-z][^>]*\bon[a-z]+\s*=/i, "内联事件属性（on*=）"],
  [/<[a-z][^>]*\bstyle\s*=/i, "内联 style 属性"],
  [/<[a-z][^>]*\b(?:href|src|action|formaction)\s*=\s*["']?\s*(?:javascript|vbscript|data:text\/html)/i,
    "危险协议 URL"],
];

const ATTACKS = [
  ['图片里写 div+img onerror', '<div><img src=x onerror="fetch(1)"></div>'],
  ['表格单元格 img onerror', "<td><img src=x onerror=alert(1)></td>"],
  ['p onclick', '<p onclick="alert(1)">x</p>'],
  ['行首远程样式表', '<div><link rel=stylesheet href="http://evil.example/x.css"></div>'],
  ['行首 iframe', '<div><iframe src="http://evil.example/"></iframe></div>'],
  ['行首 script 标签', "<script>alert(1)</script>"],
  ['行首 img（非 HTML 块）', "<img src=x onerror=alert(1)>"],
  ['内联 javascript: 链接', "[click](javascript:alert(1))"],
  ['svg onload', "<div><svg onload=alert(1)></svg></div>"],
  ['a javascript:', '<a href="javascript:alert(1)">x</a>'],
  ['img javascript: src', '<div><img src="javascript:alert(1)"></div>'],
  ['img data:text/html', '<div><img src="data:text/html,<script>alert(1)</script>"></div>'],
  ['style 属性', '<div style="background:url(javascript:1)">x</div>'],
  ['闭合标签逃逸 + img', '</div><img src=x onerror=alert(1)><div>'],
  ['属性内引号', '<div title="a" onmouseover="alert(1)">x</div>'],
  ['p 里嵌 script', "<p><script>alert(1)</script></p>"],
  ['注释藏标签', '<div><!-- x --><img src=x onerror=alert(1)></div>'],
  ['大写/空格变形', "<DIV><IMG SRC=x OnErRoR=alert(1)></DIV>"],
  ['实体编码协议 img', '<div><img src="&#106;avascript:alert(1)"></div>'],
  ['实体编码协议 a', '<a href="jav&#x61;script:alert(1)">x</a>'],
  ['base 标签', '<base href="http://evil.example/">'],
  ['srcset 注入', '<div><img src="ok.png" srcset="javascript:alert(1) 2x"></div>'],
];

const POSITIVE = [
  ["Markdown 标题", "# 标题", ["<h1>标题</h1>"]],
  ["Markdown 列表", "- a\n- b", ["<ul>", "<li>a</li>", "<li>b</li>", "</ul>"]],
  ["Markdown 粗体", "**bold**", ["<strong>bold</strong>"]],
  ["Markdown 代码块", "```\n<b>x</b>\n```", ["<pre><code>&lt;b&gt;x&lt;/b&gt;</code></pre>"]],
  ["管道表格回退", "| a | b |", ['class="tbl-row"', "<span>a</span>", "<span>b</span>"]],
  ["HTML 多行表格",
    "<table>\n<thead><tr><th>A</th><th>B</th></tr></thead>\n<tbody><tr><td>1</td><td>2</td></tr></tbody>\n</table>",
    ["<table>", "<th>A</th>", "<td>2</td>", "</table>"]],
  ["HTML colspan", '<table><tr><td colspan="2">合并</td></tr></table>', ['colspan="2"']],
  ["表内文本含 <", "<table><tr><td>a < b</td></tr></table>", ["a &lt; b"]],
  ["HTML 位置标签", "<div>hello <b>world</b></div>", ["<div>hello world</div>"]],
  ["HTML 引用块", "<blockquote>q</blockquote>", ["<blockquote>q</blockquote>"]],
  ["HTML 实体", "<p>a&nbsp;b</p>", ["a\u00a0b"]],
  ["转义实体", "<p>&lt;x&gt; &amp; y</p>", ["&lt;x&gt; &amp; y"]],
  ["合法链接", "[x](https://example.com/a)",
    ['<a href="https://example.com/a"', 'rel="noopener noreferrer"']],
  ["合法图片", "![alt](https://example.com/a.png)", ['<img alt="alt" src="https://example.com/a.png">']],
  ["data:image 图片", '<div><img src="data:image/png;base64,AAAA"></div>',
    ['src="data:image/png;base64,AAAA"']],
  ["HTML 链接保留", '<a href="https://ok.example/">x</a>',
    ['href="https://ok.example/"', 'target="_blank"']],
  ["mailto 链接", '<a href="mailto:a@b.example">x</a>', ['href="mailto:a@b.example"']],
  ["figure 内嵌 img",
    "<figure>\n<img src=\"images/a.png\">\n<figcaption>cap</figcaption>\n</figure>",
    ["<figure>", '<img src="images/a.png">', "<figcaption>cap</figcaption>", "</figure>"]],
  ["div 包多行表格",
    "<div>\n<table>\n<tr><td>1</td></tr>\n</table>\n</div>",
    ["<div>", "<table>", "</table>", "</div>"]],
  ["相对路径图片", '<div><img src="images/x.png"></div>', ['src="images/x.png"']],
];

function checkForbidden(html) {
  const hits = [];
  for (const [re, label] of FORBIDDEN) {
    if (re.test(html)) hits.push(`${label}（${re}）`);
  }
  return hits;
}

function main() {
  const ctx = loadHarness();
  let failures = 0;
  const fail = (name, detail, html) => {
    failures++;
    console.log(`FAIL  ${name}\n      ${detail}`);
    if (html !== undefined) console.log(`      output: ${JSON.stringify(html)}`);
  };

  console.log(`== 攻击 payload（${ATTACKS.length} 条，输出不得保留原始标签/属性/协议）==`);
  for (const [name, input] of ATTACKS) {
    let out;
    try {
      out = ctx.mdToHtml(input);
    } catch (e) {
      fail(name, `渲染抛异常：${e.message}`);
      continue;
    }
    const hits = checkForbidden(out);
    if (hits.length) fail(name, hits.join("; "), out);
    else if (VERBOSE) console.log(`ok    ${name} => ${JSON.stringify(out)}`);
  }

  console.log(`== 正常渲染（${POSITIVE.length} 条，必须保留内容）==`);
  for (const [name, input, musts] of POSITIVE) {
    let out;
    try {
      out = ctx.mdToHtml(input);
    } catch (e) {
      fail(name, `渲染抛异常：${e.message}`);
      continue;
    }
    const missing = musts.filter(s => !out.includes(s));
    if (missing.length) fail(name, `缺少：${JSON.stringify(missing)}`, out);
    else if (VERBOSE) console.log(`ok    ${name}`);
  }

  if (failures) {
    console.log(`\n结果：失败 ${failures} 条`);
    return 1;
  }
  console.log(`\n结果：通过（攻击 ${ATTACKS.length} 条 + 正常 ${POSITIVE.length} 条）`);
  return 0;
}

process.exit(main());
