# 03 · 后端服务与进程层（并发 / 安全 / 可观测性 / 可测试性）

> 版本：OCRMiniTool v1.2.0（`version.py`）
> 范围：`webview_server.py`(497)、`webview_backend.py`(994)、`app_webview.py`(427)、`settings_store.py`(88)、`i18n.py`(161)、`applog.py`(98)、`proc_guard.py`(102)、`motw_unblock.py`(69)、`downloader.py`（仅作交叉引用）
> 方法：**静态阅读 + 真实 HTTP 探测**。`webview_server.py`/`webview_backend.py` 是纯标准库，已用 DSH Python(3.12) 在 `127.0.0.1:0`（随机端口）实例化真实 server，settings/outputs/logs 全部重定向到 `D:\Project\_work\` 临时目录（未改动项目任何文件）。
> 证据脚本：`_work/probe_api.py`、`probe_units.py`、`probe_proc.py`、`probe_geterr.py`、`probe_keepalive.py`、`stop_test*.py`，原始输出 `_work/probe_*.log`、`_work/probe_*_results.json`。
> 标记：**【事实】**＝代码/实测可复现；**【推断】**＝代码推理，当前环境无法实测；**【建议】**＝改动提案。
> 风险等级：高 = 会丢数据/崩进程；中 = 功能错误或不可诊断；低 = 体验/整洁/纵深防御。

---

## 0. 结构速览（事实）

| 层 | 位置 | 要点 |
|---|---|---|
| 入口 | `app_webview.py:346-423` | MOTW 自清 → reexec→ 建 Job → 起 server(port=0) → 自动 load → 原生窗口/Edge → 退出 `os._exit(0)` |
| HTTP | `webview_server.py:222-486` | `ThreadingHTTPServer`（每连接一线程），静态资源 + `/api/*` + SSE |
| 业务 | `webview_backend.py:108-994` | 三任务标志位 + 单一 `RLock`；设置、历史、解释器切换 |
| 事件 | `_EventHub` `webview_server.py:194-219` | 每订阅者一个 **无界** `Queue()` |
| 配置 | `settings_store.py:22-88` | 进程内锁 + mtime/size 缓存 + 临时文件 `os.replace` |
| 日志 | `applog.py:56-98` | 仅 OCR/DOWN/MODEL/INFO/WARN/ERROR 六类，按月单文件 |
| 进程 | `proc_guard.py:34-101` | `KILL_ON_JOB_CLOSE` + `BREAKAWAY_OK` 的 Job |

实测基线（v1.2.0，无 GUI/无 torch）：`/health`→200；`/api/status`→`modelState=absent version=1.2.0`；`/api/settings`→13 键；`/api/history`→items；`/api/env`→`rec=cu128`（0.55s 硬件探测）；`/api/env` 会**写** settings.json（见 G3/E3）。

---

## 1. 并发与状态机

三任务 = `downloading / loading / recognizing / env_downloading` 四个标志（`webview_backend.py:118-121`），全部由同一把 `RLock`（`:117`）守护；任务体在 daemon 线程（`:297, 341, 449`）或 HTTP 线程内同步执行。

| 编号 | 结论 | 证据 | 等级 |
|---|---|---|---|
| A1 | **互斥正确**：20 个并发 `POST /api/download` 只有 1 个被接受（`ok_count=1 started=1`）；下载中 `/api/load`、`/api/recognize`、`/api/env/download` 全部被拒（err.busy） | 实测 C10/C3/C4/C5/C6；`webview_backend.py:258-262, 301-306, 408-416, 467-470` | 低（无问题） |
| A2 | 错误语义不统一：busy 在 `/api/download`、`/api/load` 返回 **HTTP 200** `{"ok":false}`，在 `/api/recognize` 返回 **HTTP 500** | `webview_server.py:307, 464-468`；实测 C3/C5 | 低 |
| A3 | **取消事件跨任务共享并在锁外 clear**：`start_download/start_load/recognize` 先置标志、退出锁后再 `cancel_event.clear()`（`:265, 313, 478`），`cancel()` 无条件 `set()`（`:727-730`）→ 存在"取消被 clear 吞掉"的窗口（微秒级）；且无任务时 cancel 也回 `ok:true`（实测 M7） | 实测 M7/C7/C8 | 低 |
| A4 | **SSE 队列无界**：`queue.Queue()`（`webview_server.py:202`），`publish` 用 `put_nowait` 且 `except: pass`（`:215-219`）。实测 1 个不消费订阅者灌 20k 事件 → `qsize=20000`（0.03s） | 实测 C14 | 中 |
| A5 | **断线清理慢**：handler 阻塞在 `q.get(timeout=15)`（`:406`），对端关闭后要等下一次写失败；实测 3 条 SSE 客户端关闭 20s 后线程数仍未回落（`2→5`） | 实测 L2；`webview_server.py:404-416` | 中 |
| A6 | 识别在 **HTTP handler 线程同步执行**（`webview_server.py:460`），一次识别占一个线程数分钟；客户端断开/刷新不会取消推理（无写检测）；merged 模式单请求不可中断（`ocr_engine.py` 模块注释） | 事实（代码） | 中 |
| A7 | `set_settings` 改 `modelDir` 是 **check-then-act**：`has_running_tasks()`（`:179`）与真正改 `engine.model_dir`（`:181-182`）不在同一临界区 → 任务可在检查后启动，出现"下载到新目录/加载找旧目录" | `webview_backend.py:175-194, 248-251` | 中（窗口窄） |
| A8 | 线程命名只覆盖 4 个 worker（`:297, 341, 449, 517`），HTTP handler 线程用默认名；`applog` 不记线程/pid → 并发故障无法归因 | `applog.py:56-66`；实测 S1 行格式 | 中 |
| A9 | 任务状态查询 `get_status()` 每次轮询都做文件 IO + `find_spec`（`installed_torch_info()` 读 JSON、`device_info()` 探测） | `webview_backend.py:218-246, 233, 236` | 低 |

【建议】A4/A5 一起改：`Queue(maxsize=256)` + status 事件合并（只保留最后一条）+ 5s keepalive；A6 改后台线程 + 立即 202 + 任务 id；A7 把"忙碌检查+改目录"并入同一把锁；A2 统一 `409 + {"ok":false,"error"}`。

---

## 2. 安全

### 2.1 Host / Origin 闸门

| 编号 | 事实（实测） | 谁能触发 → 能干什么 → 要用户交互吗 | 等级 |
|---|---|---|---|
| S1 | `/api/*` 与 `/health` 校验 `Host ∈ {127.0.0.1:port, localhost:port}`（`webview_server.py:265-274, 279-280`）：`Host: evil.example` → 403（S1/S3）。**静态资源不校验**（`:296 → :373`）：`GET /js/app.js` 带恶意 Host 返回 200（S4） | DNS rebinding 页面（恶意网页把域名解析到 127.0.0.1）→ 只能读前端静态文件（无秘密）→ 需受害者在浏览器打开恶意页；`/api/*` 全被拦 | 低 |
| S2 | `Origin` 仅在**存在时**校验（`:270-274`）：`Origin: http://evil.example` → 403（S2/S6/S8）；**缺 Origin 的 POST 放行** | 本机任意进程（可发 HTTP）→ 可调 `/api/shutdown`、`/api/recognize`、`/api/apply-python`→ 无需用户交互；但本机进程本来就能杀进程，收益≈0。跨站浏览器 POST 在现代 Chromium/Edge/WebView2 一律带 Origin → 被拦 | 低（远程面≈0） |
| S3 | `/api/shutdown` 无 token/无忙碌校验，直接 `os._exit(0)`（`:349-371`）。实测带正确 Host、无 Origin 的 POST 让进程 1.6s 内退出 | 同 S2；另外**应用自己的前端**只需一次误触发就会丢弃正在进行的识别/下载 | 低→中（配合 L2 有数据损失） |
| S4 | PUT/OPTIONS 未实现 → 501（实测 S10/S11），无 CORS 头 → 跨域预检必然失败（纵深防御之一） | 浏览器跨域 JSON POST 直接死 | 低 |
| S5 | 端口 `port=0`（`app_webview.py:370`）→ 端口不可预测/不可预占；调试入口固定 8770（`webview_server.py:491`） | — | 低（正向） |

【建议】S2：状态变更端点要求 `Origin` **必须存在**且合法（WebView2/Edge 均 Chromium，必带），并额外接受 `Sec-Fetch-Site: same-origin`；S1：给静态资源也加同一 `_check_host`（成本 1 行）；S3：忙时返回 `{"ok":false,"error":"busy"}` 并记日志。

### 2.2 上传 / 路径 / 临时文件

| 编号 | 事实（实测） | 判断 | 等级 |
|---|---|---|---|
| S6 | multipart 落盘名由服务端生成 `part_{n}_upload{ext}`，`ext` 只保留 `[alnum.]`≤16（`webview_server.py:131-135`）；7 种恶意文件名（`..\..\..\evil.sh`、`/abs/evil.sh`、`C:\Windows\evil.sh`、含 NUL、含引号、无扩展、超长）**全部落在临时目录内**（Q3/Q4）。解析器对 1MiB 文件 + `chunk_size=1`、截断流、声明长度大于实际流均稳健（Q5-Q7） | 无路径穿越 | 低（正向） |
| S7 | 临时目录 `tempfile.mkdtemp(prefix="ocr_web_")`（`:431`），`try/finally: rmtree(ignore_errors=True)`（`:469-470`）；实测 5 次识别后无残留（M6）。Windows 下目录 ACL 继承 `%TEMP%`，仅当前用户可读 | 可接受；`ignore_errors=True` 会静默吞掉清理失败 | 低 |
| S8 | 上传无大小上限：只校验 `Content-Length > 0`（`:424-429`），无文件数/字节上限 | 本机客户端可写满磁盘（DoS，需本机权限） | 低-中 |
| S9 | `_read_json_body` 无大小上限，`int(Content-Length)` 异常会落到 500（`:256-263`，实测 E4 回显原文 `invalid literal for int()`） | 本机小风险 + 报错信息泄露 | 低 |
| S10 | 历史读/删的穿越防护：`Path(name).name` + `_sanitize_name` + 只允许 `.md/.txt`（`webview_backend.py:768-773`），实测 `../settings.json`、`..\..\`、绝对路径、NUL 全部落空（H1/H3/H4） | 无穿越 | 低（正向） |
| S11 | **`_save_output` 会在特殊文件名上抛异常**：`aux/prn/com1` → `FileNotFoundError`、含 NUL → `ValueError`（`_sanitize_name` 只过滤 `<>:"/\|?*`，`webview_backend.py:675-679`；落盘 `:699-725`）。合并模式下异常不是 `EngineError` → 直接 500，**推理结果整体丢弃**；批量模式该异常也未被 `:644` 的 `except EngineError` 捕获 → 整批中断 | 用户选到名为 `aux.png/con.png` 的文件即可触发（无需攻击），需要用户交互 | 中 |
| S12 | `browse-file/browse-folder` 接受任意 `title/start`（`:887-910, 970-987`），在 **HTTP handler 线程**里 `tk.Tk()` 建根窗口 | 本机进程可弹任意标题的模态对话框（社工/遮挡）；tkinter 非主线程建 Tk 属未支持用法【推断】，本环境无 GUI 无法验证 | 低-中 |

【建议】S8 加 `max_upload_bytes`（如 2 GiB）与文件数上限（如 50）+ 413；S11 `_sanitize_name` 过滤 `CON/PRN/AUX/NUL/COM1-9/LPT1-9` 与 `\x00`，落盘改 `.tmp→os.replace`；S12 把对话框固定到单一 GUI 线程（或改 `GetOpenFileNameW`）。

---

## 3. 错误处理与可观测性

### 3.1 吞没清单（`except: pass` 共 25 处，逐条判断必要性）

| 位置 | 吞没内容 | 评价 |
|---|---|---|
| `webview_server.py:218-219` | `publish` 投递到某订阅者失败 | 可接受，但**订阅者异常无痕** |
| `webview_server.py:361-366` | shutdown 里写应答/停服失败 | 可接受（正在退出） |
| `webview_server.py:413-414` | SSE 对端断开 | 可接受，是主要断线路径 |
| `webview_backend.py:139-140` | `_emit` 里 `on_event` 异常 | **应记日志**：进度链路故障会完全静默 |
| `webview_backend.py:192-193` | 主题回调异常 | 可接受 |
| `webview_backend.py:785-786` | `send2trash` 未安装 | 可接受（有回退） |
| `webview_backend.py:960-961, 995-996` | flush / cancel_all | 可接受 |
| `app_webview.py` 11 处 | 标题栏/图标/注册表/窗口装饰 | 可接受（装饰性） |
| `applog.py:51-53, 65-66` | 打开/写日志失败 | 设计如此（日志永不抛） |

### 3.2 崩溃与自恢复

| 编号 | 事实 | 影响 | 等级 |
|---|---|---|---|
| E1 | **全仓无 `sys.excepthook` / `threading.excepthook` / `faulthandler`**（grep 证据）；发行版 `console=False`（`OCRMiniTool.spec:111`）→ 冻结后 `sys.stderr` 为空 → 所有 `print`/traceback 静默丢弃【推断，PyInstaller windowed 语义】 | 崩溃无痕：只有 `logs/app-*.log` 里的业务日志，无崩溃栈、无退出原因 | 中 |
| E2 | **GET 处理器完全没有 try/except**（`webview_server.py:277-296`，仅 POST 有 `:352-353`）。实测把 settings.json 变成不可写后，`GET /api/env` 让客户端收到 `RemoteDisconnected`（**无 HTTP 应答**），traceback 只进 stderr | 前端表现为"请求挂了/转圈"，日志无记录 | 中 |
| E3 | **读路径写配置**：`GET /api/env` → `_torch_flavor()` → `settings.update({"torchFlavor": ...})`（`webview_backend.py:345-361`，被 `:385` 调用）；实测 `torchFlavor` 被无提示写进 settings.json | 只读接口失败即 500（E2 的根因）；只读介质（如装在 Program Files）下必挂 | 中 |
| E4 | 客户端断连时 `socketserver.handle_error` 向 stderr 打整段 traceback（实测出现 3 次 `Exception occurred during processing`） | 发行版不可见；开发时噪音大 | 低 |
| E5 | **keep-alive 请求体未读即早退**（403/400 路径 `:267-269, 421-429`），残留 body 被当作下一条请求：实测同连接第二个请求收到 `400`，再下一个收到 `501` + 英文 HTML 错误页 | 浏览器复用连接时会莫名失败（Chrome 会复用） | 中 |
| E6 | 无看门狗/自恢复；下载残档靠 `_download_file` 的 Content-Length 对账 + Range 续传（`downloader.py:151-217`），这部分是可靠的；识别/加载无断点 | — | 低 |

### 3.3 日志覆盖矩阵

| 事件 | 是否记录 | 证据 |
|---|---|---|
| HTTP 请求/响应 | ❌ `log_message` 直接 `pass` | `webview_server.py:242-243` |
| 403/400/500 拒绝 | ❌ | 同上 + `:352-353` 只回包 |
| 模型下载/组件下载/加载/识别 | ✅ | `webview_backend.py:281, 288, 323, 326, 431, 440, 520, 531, 601, 642, 653, 663` |
| 任务取消 | 部分（下载/批量有，识别取消 ✅ `:544`） | — |
| 设置变更 | ❌ | `:175-194` 无日志 |
| 解释器切换 | ✅ `:937` | — |
| 窗口回退 Edge / WebView2 缺失 | ❌ 只 `print`（发行版不可见） | `app_webview.py:302, 403` |
| 启动/退出（版本、pid、端口） | ❌ | `app_webview.py:398-399` 只 print |
| 证书降级下载 | ✅ `log_warn` | `downloader.py:183-184` |

格式：`2026-11-12 09:43:49 [INFO] msg`（实测 S1），**无 pid/线程名/版本**，按月单文件、无大小上限与保留策略（`applog.py:30-53`）。

### 3.4 语言混淆（i18n 与日志）

| 编号 | 事实 | 建议 |
|---|---|---|
| E7 | 后端 SSE 文案走 i18n（实测 `lang=en` 时取消文案为英文，I7），但**日志文案是硬编码中文**（`:284, 288, 544` 等 12 处） | 日志保持单一语言（建议英文 key + 参数）便于检索；界面文案继续 i18n |
| E8 | `do_POST` 兜底把 `str(e)` 原样回前端（`webview_server.py:352-353`）→ 英文 Python 错误混入中文界面（实测 E4） | 包一层 `tr("err.internal")` + 详情放日志 |
| E9 | 目标解释器 stderr 用 `utf-8` 解码（`webview_backend.py:870-874`），而 zh-CN Windows 上子进程按 GBK 输出 → 界面错误乱码（实测探测 `where.exe` 输出 `��Ϣ: ...`） | 先 `utf-8`，失败回退 `locale.getpreferredencoding()` |
| E10 | i18n 静态完整性良好：zh/en 各 236 键、**0 缺失键**、25 个未使用键；缺参保留 `{name}`（实测 I3） | 可加"未使用键"清理（低优先） |

---

## 4. 可测试性

> **现状（Lead 已落地，2026-11-12）**：`tests/`（101 用例，含 `test_server_smoke.py` 真起随机端口、`test_backend_pure.py`、`test_settings_store.py`、`test_i18n.py`、`test_downloader.py`）与 `tools/check.py` 静态体检已存在——下表中 parser/SettingsStore/i18n/纯函数多数**已被覆盖**，本节仅标注"本次实测已自行验证过、可作为双保险或补充用例"的范围，以及**仍缺口**的三项：①`_EventHub` 队列上限行为（A4/C5）②`_save_output` 特殊文件名（S11/C7）③GET 处理器异常兜底（E2/C3）。

### 4.1 可直接单测（本次已实测跑通的清单）

| 目标 | 位置 | 备注 |
|---|---|---|
| `_sanitize_name` | `webview_backend.py:675-679` | 纯函数；实测 R1 |
| `_md_to_plain` | `:681-697` | 纯函数；实测 R2/R3（含 `None`/空/`<script>`） |
| `_history_path` | `:768-773` | 依赖模块常量 `OUTPUT_DIR`，注入后可测；实测 R4 |
| `normalize_python_path` | `:799-818` | 需临时目录；实测 R5/R6 |
| `_parse_multipart_stream` | `webview_server.py:62-177` | 用 `io.BytesIO` 即可，无需 server；实测 Q1-Q8（含 chunk=1 压力） |
| `fit_window_to_screen` / `_resolve_dark` | `app_webview.py:56-65, 96-101` | 纯函数，monkeypatch `_workarea_logical`；实测 R7/R8 |
| `i18n.tr/set_lang/known_langs/available_languages/detect_lang` | `i18n.py:68-161` | 实测 I1-I6 |
| `SettingsStore`（read/update/remove/损坏/并发/原子性） | `settings_store.py:22-88` | 实测 P1-P13 |
| `_EventHub`（publish/subscribe/坏订阅者） | `webview_server.py:194-219` | 实测 H1-H3 |
| `applog`（格式/并发/不可写降级） | `applog.py:56-98` | 实测 S1-S4 |
| `_recycle_file` | `webview_backend.py:775-796` | 临时目录可测（实测 H5 走 PowerShell 回退） |

### 4.2 需重构才能测

| 目标 | 阻碍 | 最小改法 |
|---|---|---|
| `WebBackend` 全部行为 | `__init__` 直接读真实 `settings.json`、构造 `OcrEngine`、写 `OUTPUT_DIR`（`:111-133, 41-44`） | 让 `settings_file/output_dir/engine` 可注入（默认走模块常量） |
| `_check_host` / `_static` / `_sse` | 它们是 `start()` 内闭包（`:239-470`），只能起真 server 测 | 抽出 `host_ok(host, origin, port) -> (bool, reason)` 纯函数 |
| `reexec_if_custom_python` | 内联 `os._exit(0)`（`:103`） | 抽 `_hard_exit(code)` 单点，测试可替换 |
| `_restart_into` | 同上（`:960`） | 同上 |
| `probe_python` / `browse_*` / `apply_python` | 真子进程 / Tk 对话框（`:820-987`） | probe 脚本已可注入 `subprocess.run`；browse 需抽 dialog provider |
| SSE 广播语义 | 依赖线程 + socket | 已有 `_EventHub` 可测；handler 层建议 e2e（本次脚本即模板） |

【建议】把本次 6 个 `_work/probe_*.py` 收敛成 `tests/test_backend_units.py`（纯函数 + SettingsStore + parser + EventHub，全部无需 GUI/torch），预计 5 分钟跑完；HTTP 面用一个 fixture（`WebViewServer(port=0)` + 临时目录）覆盖约 30 个用例——本次已证明可行。

---

## 5. 设置与配置（settings_store）

| 编号 | 事实（实测） | 影响 | 等级 |
|---|---|---|---|
| G1 | 原子写成立：模拟"磁盘满"在 `os.replace` 前失败 → 原 `settings.json` 完好，仅残留 `settings.json.tmp`（P9/P10） | 设计正确 | 低 |
| G2 | **损坏后无恢复**：截断的 JSON → `read()` 返回 `{}` 且不缓存（P5/P6），下一次 `update()` 只把补丁写回去 → 原有 `modelDir/lang/theme` **永久丢失**（P7 实测） | 用户手改坏一次即丢全部设置 | 中 |
| G3 | 进程内并发安全：8 线程 × 25 次 `update` 全部保留（202 键，P2）；跨进程无保护（见 L1） | — | 低 |
| G4 | 未知键在 `set_settings` 被白名单过滤（`webview_backend.py:176`，实测 E2）；但**值不校验**：`devicePref="garbage-value"`、`outFormat="xyz"`、任意 `scale` 都写盘（实测 E3） | 脏值只有 `devicePref` 在加载时回退，`outFormat` 任何非 `txt` 都当 `md`（`:702`） | 低-中 |
| G5 | mtime+size 缓存签名（`settings_store.py:40-43`）对"同尺寸同时间戳的外部改写"失效（实测 P8：返回旧值） | 极小概率；注释已承认一半 | 低 |
| G6 | `pythonPath` 迁移风险：`normalize_python_path` 只验"存在"（实测 R6 接受 `C:\Windows\System32\where.exe`）；启动 reexec 只判"存在且 ≠ 当前"（`:83-90`）→ 实测把 `where.exe` 拉起来后 `os._exit(0)`，应用"消失"（X 实测）；`apply_python` 有探测门（`:931-934`）但 `depsOk` 只要求 torch+transformers（`:883`），**不要求 pywebview** → 可能重启进"无原生窗口"的环境静默降级 Edge | 用户手改 settings.json / venv 被替换可触发；需用户交互或环境变更 | 中 |
| G7 | `settings.json`/`outputs/` 都在 EXE 旁（`:36-44`）：装在 `Program Files` 时不可写 → 每次保存 500、历史写失败（【推断】，本机未验证） | 部署约束 | 中 |

【建议】G2 损坏时把坏文件改名 `.corrupt-<ts>` 并保留上一份 `.bak`；G4 白名单校验并回 `400`；G6 reexec 前做一次轻量 `-c "import pywebview, torch, transformers"` 探测（结果缓存 60s，与 `_probe_cache` 同手法）。

---

## 6. 生命周期与进程

| 编号 | 事实 | 影响 | 等级 |
|---|---|---|---|
| L1 | **无单实例检测**：`app_webview.main`（`:346-423`）没有命名互斥量/锁文件；端口用 0 → 两次双击可并存。后果：①两进程各自自动加载模型（`:390-392`）→ 显存/内存翻倍；②同写 `outputs/`：`_save_output` 的 `while out.exists()`（`:699-707`）是跨进程竞态 → 互相覆盖；③`settings.json` 跨进程丢更新；④同时下载模型 → 同一文件双写（`downloader.py:151-217` 直接写最终文件名）| 双击两次即可触发，无需攻击/特殊操作 | 中-高 |
| L2 | **退出不等待任务**：`/api/shutdown` 与 `main` 都用 `os._exit(0)`（`webview_server.py:355-371`，`app_webview.py:415-423`）。实测：30s 假下载进行中关服 → **1.6s 内退出、exit code 0、worker 在第 16 tick 被腰斩**，无任何"被中断"记录 | 下载/识别结果静默丢弃；见 L3 的坏状态放大 | 中 |
| L3 | **"腰斩的权重会被判为已下载"**：`quick_check` 只查 `MIN_SIZE_BYTES`=1 GiB（`downloader.py:64-70, 117-130`），而真实权重为 **4.66 GB / 1.57 GB** → 任何 ≥1 GiB 的残档都算"downloaded"；下次启动自动 `start_load`（`app_webview.py:391`）→ safetensors 解析失败/报错难懂 | 用户"下载到一半关掉/断电"即复现；需用户交互（关窗）或无交互（崩溃） | 中-高 |
| L4 | Job Object 实现正确（`KILL_ON_JOB_CLOSE` + `BREAKAWAY_OK`，`proc_guard.py:46-99`），覆盖 powershell/probe python/新解释器/Edge 子进程；但文件头注释写的是兄弟项目的 `crispasr.exe / chatllm main.exe`（`:6-7`），失败静默返回 None（`:87-102`）无日志 | 文档漂移 + 失败无痕 | 低 |
| L5 | reexec / 重启的失败回滚：`Popen` 抛 `OSError` 时保留当前进程继续运行（`webview_backend.py:100-105`，实测 X）；`_restart_into` 失败也不退出（`:950-954`） | 设计正确 | 低（正向） |
| L6 | 端口：生产 `port=0` 无冲突；`HTTPServer.allow_reuse_address=1` 在 Windows 语义下允许**同端口二次绑定**（实测两个 server 同端口都 bind 成功并各自应答）→ 别依赖"端口占用"做单实例 | 认知纠正 | 低 |
| L7 | MOTW 自清（`app_webview.py:354-358`，`motw_unblock.py:44-69`）每次 frozen 启动遍历整棵目录（实测依赖树上万文件），且连 **EXE 本体**的 MOTW 一起删（注释明说是为了让第二次启动不再弹 SmartScreen，`:13-14`） | 有意的可用性取舍，但确实移除了系统的一次安全确认；发布说明应写明 | 低 |
| L8 | 关窗确认（`app_webview.py:259-266, 285-297`）只弹按钮、不等待取消生效；merged 模式单请求不可中断 → 结果直接丢弃，日志无记录 | 用户以为"确认了就停"，实际丢失 | 中 |
| L9 | `logs/` 与 `outputs/` 无清理策略（`applog.py:30-53` 按月单文件，`webview_backend.py:733-745` 只列 50 条） | 长期使用磁盘缓慢增长 | 低 |
| L10 | 窗口装饰线程最多轮询 6s（40×0.15s，`app_webview.py:208-225`）；WebView2 缺失检测走注册表（`:234-253`）→ Edge 回退只 print（发行版不可见，见 E1/E3 表） | 降级体验不可诊断 | 低 |

【建议】L1 加命名互斥量（`CreateMutexW("Local\\OCRMiniTool")`，已存在则 `FindWindowW` 聚焦旧窗口后退出）——这是投入产出比最高的一条；L2 退出前 `cancel_all()` + 有界 `join`（下载 2s、落盘 1s）；L3 下载前把 `Content-Length` 写入 manifest，`quick_check` 用 manifest 精确校验（同时保留 1 GiB 下限兜底）。

---

## 7. 低风险可落地清单

| 编号 | 改动点（文件:行） | 收益 | 风险 | 验证 |
|---|---|---|---|---|
| C1 | 早退路径置 `self.close_connection = True` 或读空请求体（`webview_server.py:253-274, 419-429`） | 消除 keep-alive 反序列化导致的 400/501 幽灵错误（E5） | 极低 | `probe_keepalive.py` 复跑：第二个请求应 200 |
| C2 | 统一 busy 语义为 `409`（`webview_server.py:307, 464-468`） | 前端可按状态码分支，不再靠文案猜 | 低（需前端配合） | 并发 POST 测试断言 409 |
| C3 | `do_GET` 包 `try/except` + `log_error`（`webview_server.py:277-296`） | 现在 GET 失败是"无响应+无日志"（E2） | 极低 | `probe_geterr.py`：应回 500 JSON 且日志有一行 |
| C4 | 把 `_torch_flavor()` 的"推荐写回设置"移出读路径（`webview_backend.py:345-361`） | GET /api/env 只读；只读介质不再 500 | 低 | 调 `/api/env` 后 settings.json mtime 不变 |
| C5 | `Queue(maxsize=256)` + status 事件合并 + 满则丢旧（`webview_server.py:202, 211-219`） | 内存有界（C14 的 20k 增长消失） | 低 | 重复 C14 灌 20k，`qsize()<=256` |
| C6 | keepalive 15s→5s，写失败立即退出循环（`webview_server.py:404-416`） | 断线线程回收从 ~30s 降到 ≤5s（L2 实测 20s 未回落） | 低 | 关客户端后 6s 内线程数回落 |
| C7 | `_sanitize_name` 过滤 Windows 设备名/NUL；`_save_output` 改 `.tmp→replace`（`webview_backend.py:675-679, 699-725`） | 修 R9 的"结果整体丢弃"；崩溃不留半截 md | 低 | 重跑 R9/R10 |
| C8 | 下载时记 `Content-Length` 到 manifest，`quick_check` 精确校验（`downloader.py:64-70, 117-130`; `webview_backend.py:202-216`） | 修"腰斩当完整"（L3）→ 避免自动加载坏模型 | 低-中（需兼容旧目录：无 manifest 时退回 1 GiB 下限） | 手工截断权重到 1.2GB，`modelState` 应为 `absent` |
| C9 | 退出前 `cancel_all()` + 有界 join（`webview_backend.py:990-994`, `app_webview.py:415-423`, `webview_server.py:355-371`） | 下载留可续传残档、识别不静默丢弃（L2/L8） | 低（最多多等 2s） | 假 30s 下载中关服 → 退出 ≥2s 且日志有"已取消" |
| C10 | settings 损坏时改名 `.corrupt-<ts>` 并保留 `.bak`（`settings_store.py:44-53, 76-88`） | 修 P7 的"全部设置丢失" | 低 | 写坏 JSON 后 `update()` 应保住其余键 |
| C11 | applog 行加 pid/线程名/版本；启动写 header；403/400/500 与窗口回退也记日志（`applog.py:56-66`; `webview_server.py:242-243`） | 冻结版唯一的可观测量就是日志（E1） | 低 | 行正则更新 + 触发一次 403 看日志 |
| C12 | 目标解释器 stderr 先 utf-8 再 locale（`webview_backend.py:870-874`） | 修界面乱码（E9 实测） | 极低 | 复跑 V8：错误文案应为可读中文 |
| C13 | 单实例命名互斥 + 已运行则聚焦旧窗口（`app_webview.py:367` 前） | 消除双开导致的显存翻倍/输出覆盖/设置丢更新（L1） | 低（新增 ~20 行 Win32） | 连续两次启动 → 仅一个进程、一个窗口 |
| C14 | `log_message` 在 debug 开关下记一行访问日志（`webview_server.py:242-243`） | 排查前端 404/403 有据可查 | 极低 | 请求后日志出现 `GET /api/... 200` |

---

## 8. 不建议做

| 提案 | 理由 |
|---|---|
| 给本机 HTTP 加 token/登录鉴权 | 能发请求的本机进程等价于代码执行，防不住且显著复杂化；`port=0`+Host/Origin 已足够 |
| 用 `cgi`/`email` 替换手写 multipart 解析 | 现有实现实测稳健（chunk=1/截断/恶意文件名全过），`cgi` 3.13 已移除 |
| 把 server 改 asyncio 重写 | 无性能诉求（单用户本机），`ThreadingHTTPServer` 行为已验证；重写会重引入未知并发缺陷 |
| 把 `os._exit(0)` 换成正常 `sys.exit()` | 注释已记录 torch/DLL 卸载卡死的实录（`app_webview.py:416-417`）；只应在前面加"有界等待"（C9）而不是取消硬退出 |
| SSE 改成阻塞式 `put()` | 会反压任务线程，识别/下载被前端卡住 |
| 让日志文案走 i18n | 日志需要稳定可 grep/可比对；语言混用会让用户上报的日志无法检索 |
| reexec 前做完整 120s 依赖探测 | 启动变慢；用轻量 `-c import` + 60s 缓存（同 `_probe_cache` 手法）即可 |
| 给 `/api/shutdown` 加二次确认弹窗 | 关机路径应是程序化的；改"忙时拒绝 + 记日志"更合理（C9/S3） |

---

## 9. 未完成 / 待核实（卡点）

**卡在哪**：本机无 GUI（pywebview 未安装、无桌面会话）且未构建 frozen 包，所以"原生窗口/SmartScreen/双开真实 GPU 行为"这一层**只做了代码级推断**，没有实测；其余（HTTP 面、并发、SSE、设置、解析器、日志）均已用 `_work/probe_*.py` 真实跑过。以下条目请在有桌面/GPU/打包环境的机器上补验，未验证前不要按"已确认"写进 release note。

| 项 | 原因 | 建议验证方式 |
|---|---|---|
| `browse_*` 在非主线程建 Tk 的稳定性（S12） | 无 GUI（pywebview/tkinter 不可用） | 有桌面环境时连续点 5 次"选择文件" + 同时两个请求 |
| 冻结版 `console=False` 下 stderr 为 None 导致的诊断丢失（E1） | 未构建 frozen 包 | 打包后触发一次 GET /api/env 失败，确认 `logs/` 无相关记录 |
| 双开时的显存 OOM / 输出覆盖（L1） | 无 GPU、无 GUI | 真机双开 + 同时识别 |
| `Program Files` 只读场景（G7） | 未安装到受保护目录 | 把 onedir 放 `C:\Program Files\OCRMiniTool` 试保存设置 |
| Job Object 对 Edge 子进程的连带效果（L4） | 需真实窗口 | 双开 Edge 回退模式后杀主进程，看 Edge 是否被回收 |

---

## 10. 实测证据索引（原始输出见 `_work/probe_*.log`）

| 组 | 覆盖点 | 关键数字 |
|---|---|---|
| T/S1-S15 | 路由与 Host/Origin/静态/穿越 | `/api/*` 恶意 Host 403、静态 200；3 种穿越全 403/404 |
| E1-E6 | 设置体边界、keep-alive | malformed JSON 静默 `{}`；Content-Length 非数字化 500 原文 |
| M1-M7 | multipart/识别门禁 | 400/500 分支文案正确；临时目录无泄漏 |
| C1-C14 | 任务状态机/并发/SSE 队列 | 20 并发→1；qsize 20000；cancel 事件 3 条 |
| L1-L4 | SSE 线程/端口/stop | 3 连接 20s 未回收；同端口可二次绑定 |
| P1-P13 | SettingsStore | 202 键并发保留；损坏后丢键；`.tmp` 残留 |
| Q1-Q8 | multipart 解析器 | 1MiB/1 字节块 0.68s 且校验一致；恶意文件名 9/9 落 tmp |
| R1-R11 | 纯函数/落盘 | `aux/prn/com1`→FileNotFoundError；NUL→ValueError；冲突后缀正确 |
| I1-I7 | i18n/SSE 语言 | 236 键 0 缺失；en 下取消文案英文 |
| S1-S4 | applog | 400 行并发无交错；不可写目录不抛 |
| H1-H3 | _EventHub | 坏订阅者被吞；无幽灵订阅 |
| X1-X10 | reexec/apply_python | `where.exe` 被 reexec 并 `os._exit(0)`；探测门/同解释器门生效 |
| L3(proc) | shutdown 语义 | 30s 任务中关服：1.6s 退出、kill 于第 16/300 tick |
| geterr / keepalive | GET 无兜底 / 体残留 | `RemoteDisconnected`；后续请求 400→501 |
