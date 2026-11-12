# OCRMiniTool 功能拓展探查（文档 05）

- 基线：`D:\Project\OCRMiniTool` v1.2.0（工作区含未提交改动；20 个文件显示全量差异是 CRLF 假象，写本文时真实 diff 只有 `webview_backend.py` 的 1 处几行，见 `git diff --ignore-cr-at-eol`）。
- 对照：姊妹项目 `D:\Project\STTMiniTool`（`cli_mode.py` / `docs/` / `tests/` / `pyproject.toml` 齐全）。
- 证据类型标注：**[实测]** = 本机跑通（纯 Python，无 torch/模型）；**[代码]** = 读本仓代码；**[模型源码]** = 读 `models/Unlimited-OCR-2/*.py`；**[需 spike]** = 环境无 GPU/torch 无法证伪，须先在真机采样。
- 行号口径：以本次读取的工作区为准。探查期间他人已并发改过 `webview_backend.py`（+2 行），引用时同时给函数名，便于复核。
- 本文只回答"下一步能长成什么样"，不重复找 bug；每条都给出接入点、最小实现路径、验收标准。

---

## 0. 先核实三件事（后面每条提案都依赖这里的结论）

### 0.1 坐标：模型输出里有 bbox，但当前管线把它丢了 —— [实测] + [代码]

模型输出是布局块序列，块头带 bbox（0–999 归一化）：

```
<PAGE><BLOCK>title<LEVEL1>[x1,y1,x2,y2]<FORK>标题文字
```

- `ocr_postprocess.py:99-136` `_parse_blocks()` 已把 `label / bbox / level / page / merge_prev` 全部解析成 dict；第 126-135 行即 bbox 与 LEVEL 两条取值分支。
- 但 `_render_blocks()`（`ocr_postprocess.py:252-281`）只消费 `content` 与 `level`，bbox 从未进入 Markdown。
- 我用合成 raw 串实测：`_parse_blocks` 返回 `{"label":"title","bbox":[40,40,960,120],"level":1,"page":1,...}`，而 `convert()` 产物里 bbox 全无 —— 见 `D:\Project\_work\probe_postproc.py`。
- 归一化尺度交叉印证：`modeling_unlimitedocr.py:161-165` 把坐标 `/999*image_width` 复原为像素；`ocr_postprocess.py:64-65` 有个 `_scale()` 把值夹在 0–1000，但全文件未被调用（死代码），说明 0–999/1000 是既定约定。
- 图片块的坐标还有第二份冗余来源：`to_gui_markdown()`（`ocr_postprocess.py:346-353`）用正则 `page{page}_{x1}_{y1}_{x2}_{y2}.jpg` 从内容里再抠一遍坐标。

**结论：结构化输出（JSON/可搜索 PDF/裁剪落盘）不需要改模型、不需要重训，只需要加一个"第二消费者"。**

### 0.2 置信度：模型输出里没有，且不在任何一层的契约里 —— [代码] + [模型源码]

- `infer()` 只返回字符串；`return_dict=True` 时返回 dict（`modeling_unlimitedocr.py:1105-1108`、`1201-1205`），字段是 `text / token_ids / finish_reason / num_pages_out / expected_pages / time / prefill_time` —— **没有 logprobs、没有 per-block score**。
- 解码循环 `_uocr_generate()` 每步算了 `logits`（`modeling_unlimitedocr.py:1166-1176`），但随即 `argmax` 丢弃，不落任何变量。
- `ocr_postprocess.py` 全文件无 confidence/score 字样；后处理只做规则搬运。
- 唯一可用钩子：`_uocr_page_count_guard (logits, ...)` 默认每步都被调用（`modeling_unlimitedocr.py:1170-1172`，`page_count_guard=True` 为默认），本仓已有"替换模型模块全局名"的先例——`ocr_engine.py:333-341` 把 `modeling_unlimitedocr.TextStreamer` 换成自己的子类。同理可换 guard 拿到每步 logits。
- 代价：只能得到 **token 级** top-1 概率，要映射回"块级/字符级置信度"还需 token↔文本偏移对齐；且依赖下载目录里模型源码的函数名，**换模型版本即失效**（本仓模型是下载物，不在 git 里）。

**结论：产品级"置信度"判定为不可行（不重训/不改模型推理代码拿不到可靠值）；hack 级 token 概率可行但不建议进首版（见 §3）。**

### 0.3 `model.infer` 的参数面：还能支持哪些输入形态 —— [模型源码]

签名在 `modeling_unlimitedocr.py:1039-1043`，本仓只用了其中 6 个（`ocr_engine.py:467-477`）。可拓展面：

| 参数 | 现状 | 可解锁的能力 | 证据 |
|---|---|---|---|
| `image_file` 支持 **PIL.Image 对象** | 本仓只传 `str(path)`（`ocr_engine.py:469`） | 剪贴板/截图可直接进模型，免落临时文件 | `modeling:1047,1064-1071` |
| `image_file` 支持 **list（逐页）** | 已用于合并解析 | 多页一次请求（现能力已用上） | `modeling:1064-1069` |
| `prompt` 里 `<image>` 占位符**逐页排布** | 本仓只用默认整句指令 | 逐页不同指令/字段抽取（`<image>` 之间的文字会按页插入） | `modeling:1076-1082`，`531-580` |
| `crop_mode` | 只用 None/False | 单页强制 gundam / 多页强制 base | `modeling:541` |
| `base_size=1024` / `image_size=640` / `max_crops=16` | 未暴露 | 提高分辨率上限（代价：显存/时间线性涨） | `modeling:531-533,554-566` |
| `max_length=131072` / `max_new_tokens` / `max_page_tokens=20000` | 未传 | 控制超长输出；截断点可控 | `modeling:1089-1093`，`config.json:45,77` |
| `return_dict` | 未用 | 拿 `finish_reason` / `expected_pages` / `num_pages_out` / token 数/耗时 | `modeling:1105-1107,1202-1205` |
| `temperature=0.0` | 已用 | 结果确定性可复现 | `ocr_engine.py:471` |
| `save_results=True`+`output_path` | 未用 | 自动切图 + 画框图（**但对 v2 输出疑似失效**，见 E15） | `modeling:1103-1104,1207-1228` |
| `page_count_guard` / `dash_topm` / `no_repeat_ngram_size` | 未暴露 | 页数守卫可关、稀疏注意力 topm 可调（性能向） | `modeling:585-605,1128` |
| `image_file` 传空 + 纯文本 prompt | 可行但未验证质量 | 理论上可当"文本后处理器"，但模型是 OCR 训练目标，**不建议** | `modeling:1066-1067`（`n_img=0` 分支不报错） |

---

## 1. 提案总表

| 编号 | 提案 | 用户价值 | 工作量 | 风险 | 依赖现状 |
|---|---|---|---|---|---|
| **E1** | 无头 CLI：`ocrminitool ocr <文件/目录> [--json] [--out]` + `status` 子命令 | 脚本/Agent/自动化第一次可用；CI 可跑 | 2–3 人日 | 打包版 `console=False` 打印不出来，需加 console 版 EXE | 后端逻辑已就绪，缺入口与等待同步 |
| **E2** | JSON 结构化输出（页/块/层级/bbox/阅读序） | "可编程的结果"；docx/PDF/裁剪的总闸门 | 2–3 人日（含 0.5 人日采样） | 输出 token 格式非公开契约，模型升级可能变 | `_parse_blocks` 已具备，只差渲染器 |
| **E3** | 截断与页数不符检测（`finish_reason` / 实际页数） | 防"看起来成功其实少了半篇" | 3–5 人时 | 无（只加提示，不改结果） | `return_dict` 现成；合并模式页数可直接对比 |
| **E4** | 长 PDF 自动分块（每 N 页一批） | 100 页 PDF 不再单次请求憋死；进度变准 | 1–1.5 人日 | 分块处丢跨页上下文（质量轻微下降） | 一次请求全页是现状 |
| **E5** | 目录/递归扫描（后端直读路径，**免上传**） | 一次拖一个文件夹，批量场景质变 | 1–1.5 人日 | 误选整个盘 → 需文件数/深度上限 | `browse_folder` 已有；`recognize(paths)` 直接吃路径 |
| **E6** | 剪贴板粘贴图片（Ctrl+V） | 截图 → 结果，最常用路径 | 0.5–1 人日 | WebView2 剪贴板 image 类型需实测 | `addFiles(File)` 上传链路现成 |
| **E7** | PDF 指定页范围（如 `1-3,5,10-`） | 百页 PDF 只翻需要的那几页 | 0.5–1 人日 | 页码范围解析边界 | `pdf_to_images` 逐页循环，加参数即可 |
| **E8** | 可搜索 PDF（文本层写回原 PDF） | 扫描件变"可检索 PDF"，企业场景刚需 | 3–5 人日 + 0.5 人日 spike | 中文字体嵌入 + 块级框导致文本层粗糙 | 依赖 E2；pymupdf 已是可选依赖 |
| **E9** | docx 导出（标题/段落/表格） | 直接交付 Word 文档 | 2–3 人日 | 新依赖 `python-docx`+`lxml`（+12MB） | 纯 markdown 输入，可独立做 |
| **E10** | xlsx 导出（表格 → 工作表，含合并单元格） | 表格类扫描件变可计算数据 | 1.5–2.5 人日 | `openpyxl`(+4MB)；只导出表、正文丢 | 表格 HTML 已在 markdown 里 |
| **E11** | 页眉页脚去重（label 过滤） | 书籍/论文扫描件少一半噪声 | 2–4 人时 | 模型误标成 header/footer 会误删正文 | `DROP_LABELS` 是空集合，标签已解析出来 |
| **E12** | 段落去断行 + 中英混排空格 | 阅读体验直接提升 | 1–2 人日（两个纯函数） | 误改 URL/代码/LaTeX | 后处理纯规则，零依赖 |
| **E13** | LaTeX 公式归一（`\(..\)`→`$..$` 等） | 公式块不再是乱码 | 0.5–1 人日 | 段落内数学符号被误包 | 已有 `\coloneqq` 修补先例 |
| **E14** | Markdown/HTML 表格校验修复 | 表格不再缺格/错位 | 0.5–1 人日 | 修复策略可能与原文冲突 | `_fix_span_attrs`/`_merge_table_fragments` 现成 |
| **E15** | 图片/图表区域裁剪落盘（替代失效的 `save_results`） | 图表不再是"[图片区域]"占位符 | 1–2 人日 | 需 E2 的 bbox；合并模式下页图已删 | bbox 有；Pillow 已有 |
| **E16** | 批量失败重试 + 多文档合并导出 | 批量模式少一次全量重跑 | 0.5–1 人日 | 无 | 前端已保留 `state.files`；后端已拼好合并文本 |
| **E17** | 结果内搜索/替换（编辑后重存） | 长文档校对刚需 | 1–2 人日 | 替换后与 outputs 落盘不同步 | 纯前端，结果区已有原文视图 |
| **E18** | 本地 HTTP API 稳定化（固定端口/token/路径入参/OpenAPI） | 第三方工具可集成；Agent 可调 | 2–3 人日 | 鉴权新增面；契约要冻结 | 路由与 multipart 已全有 |
| **E19** | 可配置输出目录 + Obsidian 命名约定 | 直接进笔记库，不用二次搬运 | 1–1.5 人日 | 历史/打开目录/删除都假设 `outputs/` | 固定 `OUTPUT_DIR` 一处定义、5 处消费 |
| **E20** | 历史带缩略图 | 视觉找结果，不用读文件名 | 1.5–2.5 人日 | 需保留源图/缩略图，磁盘增长与隐私 | 现在只存 `.md/.txt` |

> 排除在表外、见 §3 的：真·置信度、批量任务队列持久化、文件夹拖入、纯文本"用模型再改一遍"、多 GPU/多后端、图像预处理增强。

---

## 2. 逐条展开

### E1 无头 CLI / 命令行批处理 — 可行

- **现状证据**：`app_webview.py:346-427 main()` 里没有任何 argv 解析，`sys.argv` 仅出现在重挂解释器处（`webview_backend.py:101,947`）；`downloader.py:670` 有个 `python downloader.py [目录]` 的 `__main__`，是全项目唯一的命令行先例。打包 `console=False`（`OCRMiniTool.spec:111`）。
- **设计要点**：照抄 STTMiniTool 的三条经验：① 子命令白名单 + `_cli_argv()` 保守判定（`STTMiniTool/app_webview.py:412-434`、`443-451`）；② stdout 只放结果、日志全走 stderr 的保护器（`STTMiniTool/cli_mode.py:60-67`）；③ 退出码 0/1/2/130（`cli_mode.py:483-502`）。子命令建议：`ocr <paths…> [-o out] [--json] [--mode merged|batch] [--prompt] [--device]`、`status --json`、`model`（复用 `downloader.download_model`，`downloader.py:576`）。
- **最小实现路径**：新增 `cli_mode.py`（~300 行）直接用 `OcrEngine`：`OcrEngine(model_dir).load(device_pref)` 是同步的（`ocr_engine.py:285-347`），不需要 `WebBackend.start_load()` 那层线程（`webview_backend.py:301-343` 是异步的，CLI 里反而要轮询）。模型目录/设备偏好读 `SettingsStore`（`settings_store.py`），torch 组件检测用 `quick_check_torch`（`downloader.py:304`）。`app_webview.main()` 在 `reexec_if_custom_python()`（`app_webview.py:362`）之后、起 server（`:370`）之前插 5–10 行分发。
- **验收标准**：`python app_webview.py ocr a.png --json` 的 stdout 可被 `json.loads` 一次解析成功（stderr 有进度）；`--out x.md` 与 GUI 同格式；模型未下载/组件未装时退出码 1 且 stderr 一句人话；`status --json` 不加载模型（<2s）。
- **工作量依据**：1 个新文件（~300 行）+ 入口 5–10 行 + spec 增一个 `console=True` 的 EXE target（~10 行）+ README 一节；不动前端、不加运行时依赖。**2–3 人日**（其中 0.5 人日为打包验证）。
- **风险**：`console=False` 的主 EXE 打印不出 stdout——STTMiniTool 也在同样配置下宣传 `STTMiniTool.exe transcribe --json`（`STTMiniTool.spec:46`、`README.md:127-135`），这是要**刻意不照抄**的坑：要么加 console 版 EXE（打包体积几乎不变，共享 `COLLECT`），要么 CLI 只承诺源码运行。

### E2 JSON 结构化输出（页/块/层级/bbox/阅读序）— 可行，建议紧跟 E1

- **现状证据**：`_parse_blocks()` 返回的块已含 `label/bbox/level/page/merge_prev`（`ocr_postprocess.py:99-136`），`_build_groups()` 已算出阅读序并查集聚组（`ocr_postprocess.py:156-216`），但 `convert()` 只输出文本（`ocr_postprocess.py:322-343`），引擎在 `ocr_engine.py:421,431` 调 `to_gui_markdown()` 后 raw 文本即丢弃，`recognize()` 返回体只有 `text/pages/elapsed`（`ocr_engine.py:449-455`）。
- **设计要点**：新增 `to_blocks(raw) -> list[dict]`，复用 `_parse_blocks` + `_merge_adjacent_titles` + `_build_groups`，每项 `{page, label, level, bbox(normalized 0-999), text, role(md|table|image|heading)}`；顶层 `{schema: "ocrmini/blocks@1", pages: [...], blocks: [...]}`。**必须标注 schema 版本且注明"依赖模型输出 token 方言，非稳定对外契约"**。
- **最小实现路径**：① `ocr_engine.recognize()` 返回体加 `raw: str`（合并模式已按 `<PAGE>` 切好，`ocr_engine.py:430`，把 raw parts 一并返回）；② `ocr_postprocess` 加 `to_blocks()`（~70 行）；③ `_save_output()` 支持 `fmt="json"`（`webview_backend.py:701-725`，现有写法是 md/txt 二选一，需改成枚举）；④ 设置页 `outFormat` 加一项（`index.html:205`、`app.js:755,784`）+ 两份 locale（`webview/locales/{zh,en}.json` 各 1–2 个 key）。
- **验收标准**：对一个含标题/表格/双栏的页面，JSON 里每个块都有非空 `bbox` 且 `label` 取自 `_BASE_LABELS`（`ocr_postprocess.py:47-51`）；`page` 从 1 单调递增；`convert(blocks→md)` 与现有 `convert()` 输出逐字节一致（回归保护）。
- **工作量依据**：改 4 个文件（postprocess/engine/backend/settings UI）+ 2 份 locale；不动模型；无新依赖。**2–3 人日**，其中 **0.5 人日**必须先做"真机采一次 raw 输出"的 spike（见风险）。
- **风险**：`<BLOCK>/<FORK>/<LEVEL>` 是**推断出来的**当前方言（词表里 `<BLOCK>`/`<FORK>`/`<PAGE>`/`LEVEL` 都存在，且 `to_gui_markdown` 按 `page_x1_y1_x2_y2.jpg` 抠图，`ocr_postprocess.py:9-14,61`），但词表里**同时**有旧方言 `<|ref|>/<|det|>`（见 E15）。必须先采样确认，否则 JSON 的 bbox 全部为 null。

### E3 截断 / 页数不符检测 — 可行，**性价比最高**

- **现状证据**：`_infer()` 丢掉了 `return_dict` 元数据（`ocr_engine.py:466-480` 只取 `out` 字符串）；合并模式把 raw 按 `<PAGE>` 切页后**不与输入页数核对**（`ocr_engine.py:428-431`）；模型侧其实有 `finish_reason`、`expected_pages`、`num_pages_out`（`modeling_unlimitedocr.py:1201-1205`），页数守卫还会在超页时强制 EOS（`modeling:597-605`）。
- **设计要点**：三个信号合并成一个 `warnings[]` 返回：① `len(parts) != len(expanded)` → "模型只解析了 N/M 页"；② `finish_reason == "length"` → "输出被 max_new_tokens 截断"；③ `num_pages_out != expected_pages` → "页数守卫触发"。前端结果区顶部显示一条可关闭 banner。
- **最小实现路径**：`_infer()` 传 `return_dict=True` 并把 `res` 里 3 个字段透传（`ocr_engine.py:467-480`）；`recognize()` 组装 `warnings`（`:449-455`）；`webview_backend.recognize/_recognize_batch` 原样带出（`:534-541`、`:636-641`）；`app.js` 的 `showResult()`/`renderResultText()`（`:331-356`）加 banner；2 个 locale key。**完全不改结果内容**，只加提示。
- **验收标准**：构造一个页数不匹配的 raw（或 mock 返回）时前端出现警示且 `result.warnings.length>0`；正常结果 `warnings=[]`；`--json`（若已有 E1）时字段存在。
- **工作量依据**：改 3 个 Python 文件各 3–8 行 + 前端 10 行 + 2 locale；不动 CLI/不动模型。**3–5 人时**。
- **风险**：无破坏性；唯一注意点是 `pages` 数量与前端页签逻辑（`app.js:407-414`）不能因此变成空数组。

### E4 长 PDF 自动分块 — 可行

- **现状证据**：`recognize()` 对多页只走一条路径——**全部页塞进一次 `model.infer`**（`ocr_engine.py:424-433`），没有页数上限判断；上下文上限 `max_length=131072`（`config.json:77`），每页 base 模式约 273 个图像 token（`modeling:546-547,564`，`nqb=16` → `16*17+1`），理论页数上限 ~400，但显存/内存与 CPU 时间才是真瓶颈（README:94-97：CPU 单页 3–15 分钟）。进度也因此在合并模式下只有"一条 0→0.9"（`webview_backend.py:492-501`）。
- **设计要点**：当 `len(pages) > N` 且是 CPU/低显存时，按 N 页一批（默认 N=8，随设备信息调整）分批调用 `_infer`，批间 `cancel_event` 检查点、进度按批推进，最后按页序拼接。**代价**：批边界处丢跨页上下文（跨页表格/段落可能断开），必须在 UI 注明并允许关闭。
- **最小实现路径**：只改 `ocr_engine.recognize()` 的多页分支（`ocr_engine.py:424-433`）：`for chunk in chunks(expanded, n)` → `_infer(chunk, prompt)`；`progress_cb` 用 `(done_chunk / total_chunk)`；`_build_groups`/跨页表格合并在单批内仍生效。不动前端。
- **验收标准**：20 页 PDF 在 CPU 上进度按 2–3 批推进且可取消（取消在批间 <2s）；单页输出与不分块时逐字节一致（分块关闭时行为不变）；批边界处有 `<!-- page N -->` 分隔保留（`ocr_engine.py:450-452`）。
- **工作量依据**：改 1 个文件 ~60 行 + 参数从设置/CLI 透传；无新依赖、不动前端。**1–1.5 人日**。
- **风险**：跨页上下文是产品卖点（README:18-19），分块是**质量换稳定性**，默认值要保守（建议只在页数 >16 或显存 <8GB 时启用）。

### E5 目录 / 递归扫描（后端直读路径）— 可行，**性价比最高**

- **现状证据**：前端只接受上传的 `File`（`index.html:239` `accept=…`、`app.js:167-179 addFiles`），后端 `recognize(opts.paths)` 却**只认本地路径**（`webview_backend.py:473`），HTTP 层把上传文件落到临时目录再当路径用（`webview_server.py:431-444`）。后端已有 `browse_folder()`（`webview_backend.py:972-988`）与 `/api/browse-folder` 路由（`webview_server.py:330-334`）。
- **设计要点**：前端加"选择文件夹"按钮 → `browse_folder` 拿绝对路径 → 新端点 `/api/recognize-dir`（JSON body：`{dir, recursive, exts, mode, prompt}`）→ 后端 `Path.rglob` 展开（复用 `ocr_engine.IMAGE_EXTS`，`ocr_engine.py:34`，加 `.pdf`）→ 直接调 `recognize(paths)`。**零上传、零临时拷贝**，大目录也不会把内存打爆。
- **最小实现路径**：后端新增一个 ~40 行方法（扫描 + 上限校验：默认最多 200 个文件、深度 ≤4、忽略隐藏目录）；server 加一条 POST 路由（`webview_server.py:299-350` 处 pattern 一致）；前端一个按钮 + 一个"递归"勾选；2–3 个 locale key。
- **验收标准**：选一个 50 张图的目录 → 文件列表按名称自然序显示、批量模式逐个出结果；选超过上限的目录时明确报错而不是卡死；`/api/recognize-dir` 对不存在/空目录返回 400。
- **工作量依据**：改 3 个文件（backend/server/app.js+index.html）+ locale；不动引擎；无新依赖。**1–1.5 人日**。
- **风险**：`_check_host()` 只挡 DNS rebinding（`webview_server.py:265-274`），本机任何进程都能调这条新端点读任意目录——建议限制为"只允许 `browse_folder` 返回过的路径"或至少加 E18 的 token。

### E6 剪贴板 / 截图粘贴（Ctrl+V）— 可行

- **现状证据**：前端只有 dragover/drop（`app.js:159-165`）与文件选择框，**全项目没有 paste 监听**（`grep paste webview/` 只命中文本复制 `app.js:450`）。上传链路对 `File` 无差别处理（`bridge.js:165-176` 拼 `FormData`），后端 `_parse_multipart_stream` 按后缀落盘（`webview_server.py:62-190`）。
- **设计要点**：`window.addEventListener("paste")` → 遍历 `clipboardData.items`，`kind==="file"` 且 `type.startsWith("image/")` → `getAsFile()` 塞进 `addFiles([file])`（自动走现有上传/识别链路）。落盘名建议 `clipboard-<timestamp>.png`，扩展名按 MIME 映射。
- **最小实现路径**：只在 `app.js` 加 ~25 行 + 1 个提示 locale（"已从剪贴板添加图片"）；后端零改动。
- **验收标准**：截图后 Ctrl+V，文件列表出现新项且可识别；粘贴多张图按粘贴顺序入列；粘贴文本（非图片）不误触发。
- **工作量依据**：1 个前端文件；不动后端；无新依赖。**0.5–1 人日**（0.5 人日为 WebView2 上验证 `clipboardData.items` 行为）。
- **风险**：WebView2/Edge 对 `paste` 事件里图片的支持要看具体版本，需实测；HTTP 非安全上下文不影响（`http://127.0.0.1` 被视为安全源）。

### E7 PDF 指定页范围 — 可行

- **现状证据**：`pdf_to_images()` 对 `for i, page in enumerate(doc)` 全量转图（`ocr_engine.py:350-370`），无页码参数；调用点 `ocr_engine.py:406-412` 也不传；`recognize()` 的 `opts` 只带 `paths/names/prompt/mode`（`webview_server.py:451-458`）。
- **设计要点**：`pdf_to_images(pdf, dpi, out_dir, pages=None)`，`pages` 用 0-based 区间列表；表达式解析（`1-3,5,10-`）放后端，前端只给一个输入框。**注意**：页码是"原 PDF 页码"，分块（E4）与页码范围是两回事，别混用。
- **最小实现路径**：引擎加 `pages` 参数（`ocr_engine.py:350-370`，`for i, page in enumerate(doc)` 前加索引过滤）→ `recognize(image_paths, …, pdf_pages=None)` → `opts["pdfPages"]` → server 透传（`webview_server.py:451-458`）→ 前端识别页一个输入框 + 2 locale。
- **验收标准**：`1-3,5` 只出 4 页结果且页标签编号对应原 PDF 页码（1,2,3,5）；非法表达式报中文错误；空选择（如 `99-`）明确报"该范围无页"。
- **工作量依据**：改 4 个文件、每处 5–20 行；无新依赖。**0.5–1 人日**。
- **风险**：页码范围与"合并解析"一次请求的页序必须一致，否则跨页上下文错乱；页标签编号要额外存"原页码"而不是数组下标。

### E8 可搜索 PDF（文本层写回）— 有条件可行（依赖 E2）

- **现状证据**：`pymupdf` 已是可选依赖（`requirements.txt:50`），`fitz` 在 `pdf_to_images` 里已用于渲染（`ocr_engine.py:354-370`，dpi=200）；bbox 有（§0.1），但页图在推理后即删（`ocr_engine.py:434-440`）。
- **设计要点**：识别完成后按页重建：`fitz.open(原PDF)` → 对每页 `page.insert_textbox(rect, text, fontname=…)`，坐标由 `bbox/999 * page.rect` 换算（见 `modeling:161-165` 的同款换算）；文本设为不可见（`render_mode=3`）即"可搜索但不破坏版面"。**可选**：把 image 块的裁剪图按 bbox 贴回。
- **最小实现路径**：① 先做 **0.5 人日 spike**：在真机 sample 上确认 `fitz.Font("china-s")`/内置 CJK 字体可用、`insert_textbox` 对中文不抛 `font` 错误；② 新模块 `pdf_textlayer.py`（~150 行）；③ 后端加 `fmt="pdf"` 分支（`_save_output` 需要"原 PDF 路径"入参，现在只拿 name+result，须扩展契约）；④ 设置页加一项。
- **验收标准**：输出 PDF 在 Acrobat/Edge 里 Ctrl+F 能命中中文关键词；原始页面视觉与原 PDF 一致（文本层不可见）；页数与原 PDF 相等。
- **工作量依据**：1 个新模块 + 后端 1 处契约扩展 + 设置 UI + 2 locale；**新依赖**：pymupdf 从"可选"变"导出 PDF 时必需"（已是 requirements 里的 optional，打包需确认 spec 收录，`OCRMiniTool.spec:38-50`）。**spike 0.5 人日 + 实现 3–5 人日**。
- **风险**：① CJK 字体嵌入是本方案唯一硬风险，未在无 fitz 环境证伪；② 块级 bbox 只有"块框"，长段落文本塞进块框会溢出 → 需要"按 bbox 高度估字号/换行"或直接按块分行放；③ PDF 若加密/有权限位，写回会被拒。

### E9 docx 导出 — 可行，[实测] 原型已跑通

- **现状证据**：导出只有 md/txt 两条（`_save_output` `webview_backend.py:701-725`；前端保存按钮 `app.js:476-495`）；`requirements.txt` 无 python-docx。**已实测**：用 bundled python 的 `python-docx 1.2.0`，把 `convert()` 产物（`# 一级标题` + 原生 `<table>` + 段落）在 ~60 行内转成 docx，标题落到 `Heading 1`、表格含 `colspan=2` 合并单元格（`D:\Project\_work\probe_exports.py` → `out\probe.docx`，36,920 字节，回读校验 4 段/1 表）。
- **设计要点**：层级直接映射 `#{n}` → `doc.add_heading(level=n)`（模型输出已经是 `LEVELn` → `#`，`ocr_postprocess.py:270-276`）；HTML 表格用现有 `_fix_span_attrs` 规整后的 `colspan/rowspan`（`ocr_postprocess.py:68-77`）驱动 `merge_cells`；图片块可留占位段落（或配合 E15 插图）。
- **最小实现路径**：新 `export_docx.py`（~120 行，md→docx 子集：标题/段落/列表/表格/代码块）；`_save_output` 加 `fmt="docx"` 分支；设置页 `outFormat` 三选一（`index.html:205`）+ `app.js:755,784` + 2 locale；`requirements.txt` 加 `python-docx>=1.1`。
- **验收标准**：识别一个含表格的页面 → 导出 docx 用 Word 打开无修复提示；标题层级与 Markdown 一致；表格行列数与原表一致（含合并单元格）；单文件 <500KB 页面 <3s。
- **工作量依据**：1 个新模块 + backend 1 分支 + 设置 UI + locale + 依赖声明；**新依赖体积实测**：`docx` 3MB + `lxml` 9MB（原生扩展，PyInstaller 需验证钩子）+ `et_xmlfile` 1MB ≈ **+13MB 解包体积**。**2–3 人日**。
- **风险**：① `lxml` 是 C 扩展，PyInstaller onedir 打包需按现有 `binaries` 处理逻辑确认（`OCRMiniTool.spec:50-70` 已有类似收集）；② 备选"零依赖手写 OOXML（zipfile）"约 250–300 行，可省依赖但表格合并/样式更易出错——建议先用 python-docx，打包体积不可接受时再降级。

### E10 xlsx 导出（表格 → 工作表）— 可行，[实测] 原型已跑通

- **现状证据**：表格在 markdown 里是**原生 HTML**，且从 `_render_blocks` 原样输出（`ocr_postprocess.py:277-279`）；跨页表头去重与 `colspan/rowspan` 规整已实现（`ocr_postprocess.py:219-249,68-77`）。**已实测**：`openpyxl 3.1.5` 把 `convert()` 产出的 `<table>` 写成 3 行工作表并应用 `merge_cells`（`out\probe.xlsx`，4,927 字节，回读 `A1=项目`、`B3=0.5`）。
- **设计要点**：多表 → 多 sheet（`表1/表2`）或一个 sheet 纵向堆叠（推荐后者，便于 Obsidian/Excel 复制）；只导出表，正文另存 md（导出前提示"仅表格"）。
- **最小实现路径**：新 `export_xlsx.py`（~100 行：`TR_RE/CELL_RE/SPAN_RE` + openpyxl）；`_save_output` 加 `fmt="xlsx"`；设置 UI + locale；`requirements.txt` 加 `openpyxl>=3.1`。
- **验收标准**：含 2 张表的页面 → xlsx 有 2 个 sheet；合并单元格位置正确；数值型单元格（如 `0.5`）是数字而非文本（可选：`float()` 试探）；无表格时明确报错而不是产出空文件。
- **工作量依据**：1 个新模块 + backend 1 分支 + UI + locale；**新依赖** openpyxl 3MB（纯 Python）。**1.5–2.5 人日**。
- **风险**：① 只导出表格会丢掉正文，用户预期要管理好；② `openpyxl` 写大表（>5 万格）会慢，需加行数上限。

### E11 页眉页脚去重（label 过滤）— 可行，成本极低

- **现状证据**：`DROP_LABELS = set()`（`ocr_postprocess.py:41`）——空集合，意味着 `_render_blocks` 的丢弃分支（`:268-269`）永不触发；而 `header / footer / page_number` 都在 `_BASE_LABELS` 里（`:47-51`）。**[实测]**：合成 raw 里 `<BLOCK>header…` 与 `<BLOCK>footer…` 经 `convert()` 后原样留在输出里（`probe_postproc.py` 第 2 段）。
- **设计要点**：给 `convert()` 一个 `drop_meta=False` 开关；开启时把 `header/footer/page_number` 从渲染中剔除，并对"跨页重复率 >60% 的 header/footer 文本"做二次去重（防模型把重复页眉标成 `text`）。默认关，设置页可开（保守：不改变既有行为）。
- **最小实现路径**：`ocr_postprocess.convert(result, drop_meta=False)`（`:322-343`）+ `to_gui_markdown(raw, drop_meta=…)`（`:346-353`）+ `ocr_engine.recognize` 透传 + `opts["dropMeta"]` + 设置项（`DEFAULT_SETTINGS` 加 1 键，`webview_backend.py:46-56`）+ 2 locale。
- **验收标准**：开关前后对比：书籍扫描件输出的首尾重复行消失；正文中偶然出现的"页眉"字样的 `text` 块**不被误删**；PDF 多页时跨页重复行只留一次。
- **工作量依据**：改 3 个 Python 文件各 5–15 行 + 1 个设置项；无新依赖、不动引擎。**2–4 人时**。
- **风险**：模型把正文首行标成 `header` 时会误删——所以默认关 + 二次去重必须"重复率"证据；`DROP_LABELS` 是上游移植时留的语义位，改动要写清注释。

### E12 段落去断行 + 中英混排空格 — 可行

- **现状证据**：块内文本直接拼接（`ocr_postprocess.py:280` `"+''.join(contents)"`，无分隔符、无断行修复）；`convert()` 末尾只有空行压缩与坐标行清理（`ocr_postprocess.py:337-343`），**没有** de-hyphenation、没有 CJK/拉丁之间空格。
- **设计要点**：两个纯函数（新增 `text_normalize.py`，或放 `ocr_postprocess.py` 尾部）：① `dehyphenate("exam-\nple"→"example")` + CJK 行尾/行首软换行合并；② `cjk_latin_spacing()` 在中英之间补一个空格，**跳过**行内代码、URL、`$…$` 公式、表格 HTML。默认开启②、①可选。
- **最小实现路径**：纯字符串处理，无依赖；`convert()` 末尾挂载；加开关（设置项）+ 单测（可直接放 `tests/test_ocr_postprocess.py` 风格，姊妹项目已有该文件）。
- **验收标准**：`"OCR识别"`→`"OCR 识别"`；`"https://a.com/b"` 与 `` `code_x` `` 不被插入空格；`"exam-\nple"`→`"example"`；对同一样例重复执行幂等（第二遍无变化）。
- **工作量依据**：1 个新模块 ~120 行 + 1 处挂载 + 单测 30–50 行；无依赖、不动前端（默认开需加设置项与 locale，可先只做后台默认）。**1–2 人日**（两件事各约 0.5–1 人日）。
- **风险**：中英混排规则天然有"该不该加空格"的争议（数字+单位、中文标点）；必须提供开关与"仅影响新结果"的说明。

### E13 LaTeX 公式归一 — 可行

- **现状证据**：`equation` 在 `_BASE_LABELS` 内（`ocr_postprocess.py:47-51`），但渲染时按普通文本处理（`:270-280` 无 equation 分支）；已有 `\coloneqq`/`\eqqcolon` 修补先例（`:337`）。
- **设计要点**：① 把 `\(…\)`/`\[…\]` 归一为 `$…$`/`$$…$$`（GitHub/Obsidian 都吃这种）；② `\begin{equation}` 包裹转 `$$`；③ 常见误识别修补（如 `\frac12`→`\frac{1}{2}`、`^` 后单字符补 `{}`）；④ **`equation` 块若内容含 `$` 则原样保留**（幂等）。
- **最小实现路径**：`normalize_math()` ~60 行 + 挂到 `convert()` 尾部；无新依赖。
- **验收标准**：`E = mc^2` 在公式块里能被渲染器识别为公式（或至少 `$…$` 包裹）；普通正文中的 `$100` 不被误包；二次执行无变化。
- **工作量依据**：1 个文件 ~60–80 行 + 单测。**0.5–1 人日**。
- **风险**：模型输出的公式块里可能混排中文说明，整块包 `$` 会让"文字变斜体"——建议只包裹"命中 `\` 或 `^`/`_` 的行"。

### E14 Markdown/HTML 表格校验修复 — 可行

- **现状证据**：已有 `_fix_span_attrs`（`ocr_postprocess.py:68-77`）修 `colspan="x"` 非数字、`_merge_table_fragments`（`:219-249`）做跨页表头去重，但**没有**：单元格数不齐、`<td>` 未闭合、空表、只有一行表头。
- **设计要点**：校验器输出 `warnings`（不静默改内容）：每行 `cells` 数 vs 表头列数、`<table>` 内行数 <2、`colspan/rowspan` 之和超列数；修复分级——安全修复（补 `</td>`、丢弃空表）默认做，风险修复（补空单元格）只在开关下做。
- **最小实现路径**：`ocr_postprocess.py` 加 `validate_tables(md) -> list[dict]`（~80 行，复用现有 4 个正则）；接 E3 的 `warnings` 通道显示；单测。
- **验收标准**：构造缺格/超列/空表三类样例，warnings 文案准确；合法表格零误报；修复后 `_fix_span_attrs` 与新校验器都过。
- **工作量依据**：1 个文件 ~80 行 + 单测 + 复用 E3 的前端 banner（若 E3 未做则需 `warnings` 通道）。**0.5–1 人日**。
- **风险**：模型输出的 HTML 表格**本身就是 Markdown 里合法内容**，任何"自动修复"都可能与原文冲突——所以默认只校验+提示，不改写。

### E15 图片/图表区域裁剪落盘 — 可行，但**不能直接用 model 的 `save_results`**

- **现状证据**：`infer(save_results=True)` → `_uocr_save_results()` 会切图并画框（`modeling_unlimitedocr.py:1207-1228`、`130-199`），但它依赖 `re_match()` 的 `<|ref|>…<|/ref|><|det|>[x1,y1,x2,y2]<|/det|>` 旧方言（`modeling:98-113`）。本仓后处理处理的是 `<BLOCK>` 新方言（`ocr_postprocess.py:9-14`），两者 token 都在词表里（`tokenizer.json` 可 grep 到 `<BLOCK>/<FORK>/<PAGE>` 与 `<|ref|>/<|det|>/<|det|>`），**所以 `save_results` 对 v2 输出大概率等于空跑**。而 bbox 本身是有的（§0.1），`to_gui_markdown` 目前只把它变成斜体占位符（`ocr_postprocess.py:346-353`）。
- **设计要点**：自己实现：`Pillow` 打开原页图 → 按 `bbox/999 * (w,h)` 裁剪 image/figure/chart 块 → 存 `outputs/<name>_assets/page{p}_img{k}.png` → markdown 里把占位符替换成 `![](assets/…)`（**与 GUI 预览一致，`HTML_BLOCK_RE`/图片链接已在 mdToHtml 支持，`app.js:265,279`**）。
- **最小实现路径**：依赖 E2（要 raw + bbox）；`to_gui_markdown(raw, image_saver=callable)` 或新 `extract_images(raw, page_images)`；合并模式下页图在临时目录、推理后即删（`ocr_engine.py:434-440`）→ 需在删除前复制需要的页或改为"边识别边裁"。**~100 行**。
- **验收标准**：含 2 张图的一页 → `outputs/<name>_assets/` 出现 2 个 png，尺寸与 bbox 换算一致（±2px）；markdown 里原来的 `*［图片区域…］*` 变成可点开的本地图片；无图页面不产生空目录。
- **工作量依据**：改 1–2 个文件 ~100 行 + 输出目录约定（history 扫描要能忽略 `_assets/`，`webview_backend.py:735-745`）；Pillow 已是依赖。**1–2 人日**（含 0.3 人日方言语义核实）。
- **风险**：① 必须先在真机采样确认 v2 的 image 块内容是 `![](pageX_…)` 还是别的（决定"用内容里的坐标"还是"用块 bbox"）；② PDF 页图 dpi=200 与 bbox 换算是图像坐标，不能直接当 PDF 点用。

### E16 批量失败重试 + 多文档合并导出 — 可行，成本极低

- **现状证据**：批量模式已保留每项 `{name, ok, text, pages, outPath, elapsed, chars}`（`webview_backend.py:636-641`、前端 `app.js:361-390`）；前端 `state.files` 是原始 `File[]`（`app.js:175`），与 `items` 下标一一对应；后端其实**已经算好了合并文本** `"\n\n".join(ok 项 text)`（`webview_backend.py:666-671`）——但从未落盘。
- **设计要点**：① 结果面板加"重试失败项"按钮：`files.filter((_,i)=>!items[i].ok)` 重新 `API.recognize({files: failed, mode:"batch"})`；② 加"合并导出为一份 md"：直接用后端返回的 `result.text`（或前端 `items.map(it=>it.text).join`），保存到 outputs。
- **最小实现路径**：纯前端 ~60 行（`app.js` 结果区）+ 1 个 locale key；后端零改动（合并文本已在响应里）。若要落盘到 outputs 则后端加一个 `save_text(name, text)` ~15 行。
- **验收标准**：批量 5 项失败 2 项 → 点重试只跑 2 项且成功后列表就地更新；合并导出文件包含全部成功项、项间有分隔标题；重试时不重复已完成项。
- **工作量依据**：1 个前端文件 + 可选 1 个后端小方法；无依赖。**0.5–1 人日**。
- **风险**：重试期间 `state.running` 与进度条要复用现有状态机（`app.js:215-219`），否则会双跑。

### E17 结果内搜索/替换 — 可行

- **现状证据**：结果区只有"预览/原文"两态（`app.js:340-356`），原文是 `readonly` 的 textarea（`index.html:123`）；复制/保存已存在（`app.js:443-495`）；无任何搜索框。
- **设计要点**：在结果卡片加搜索框：命中高亮（浏览器 `window.find` 太弱，建议在原文态做 `innerHTML` 分段包裹 `<mark>`）；替换仅对 `state.lastResult.text/pages` 做字符串替换后重渲染，并在保存时生效（**不自动覆盖 outputs 已落盘文件，需显式"保存"**，避免毁掉原始结果）。
- **最小实现路径**：`app.js` + `app.css`（高亮样式）+ 3–4 个 locale key；~120 行前端，零后端。
- **验收标准**：搜"表格"能定位并高亮全部命中、上下条跳转；替换后预览同步更新、`保存` 写出的是新文本、`outputs/` 原文件未被自动改写；多页结果按当前页作用域替换。
- **工作量依据**：2 个前端文件；无依赖。**1–2 人日**。
- **风险**：`escapeHtml` 与 `<mark>` 包裹顺序易出 XSS/破标签，需在转义后对文本节点做包裹；替换作用于 Markdown 源而非渲染 HTML（否则会改坏 `<table>`）。

### E18 本地 HTTP API 稳定化 — 可行

- **现状证据**：已有 21 条 `/api/*` 路由 + `/health`（`webview_server.py:277-350`），`/api/recognize` 只收 multipart 上传（`:419-470`）且**同步阻塞**（模型跑几分钟，HTTP 线程一直挂着）；端口是 `port=0` 随机（`app_webview.py:370`），只把 URL `print` 到控制台（`:398-399`，而打包版 `console=False` 看不到）；安全只有 Host/Origin 校验（`webview_server.py:265-274`），**无 token**；无 OpenAPI/契约文档；`/api/events` 是全局广播，多客户端会互抢进度（`:390-416`）。
- **设计要点**（按性价比排序）：① `--port` 参数 + 固定端口回退（或写 `%LOCALAPPDATA%\OCRMiniTool\port` 供第三方发现）；② `X-OCR-Token` 头鉴权（token 启动生成、写端口文件旁）；③ 新增 `POST /api/recognize-paths`（JSON：`{paths, prompt, mode, pdfPages, dropMeta}` → 直接 `backend.recognize`，**免上传**，与 E5 同源）；④ 异步任务：`POST /api/jobs` → `{jobId}`，`GET /api/jobs/{id}` 轮询，复用现有 `cancel_event`；⑤ `GET /api/openapi.json` 手写静态 schema（不引依赖）。
- **最小实现路径**：server 加 3–4 个路由 + 参数解析（~150 行）；`app_webview.main()` 解析 `--port`（~10 行）；OpenAPI 静态 JSON（~80 行，可手写）。不动引擎。
- **验收标准**：`curl -H "X-OCR-Token: …" -X POST /api/recognize-paths -d '{"paths":["a.png"]}'` 返回与 GUI 相同结构；错误 token 403；无 `--port` 时行为与现在完全一致（回归）；`/api/openapi.json` 可被 Swagger UI/`openapi-generator` 解析。
- **工作量依据**：2 个后端文件 ~250 行 + OpenAPI 静态文件；无新依赖。**2–3 人日**。
- **风险**：① 鉴权是新的信任边界，token 文件权限要收紧（`%LOCALAPPDATA%` 默认 ACL 尚可）；② 契约一旦对外就要冻结，`text/pages/outPath` 字段名不能随便改（`webview_backend.py:534-541`）；③ 异步 job 与"同一时刻只能一个识别"（`:467-470`）的互斥语义要写清楚（返回 409 而不是排队）。

### E19 可配置输出目录 + Obsidian 命名约定 — 可行

- **现状证据**：`OUTPUT_DIR = BASE_DIR / "outputs"` 固定（`webview_backend.py:43`），并硬编码被 4 处消费：`_save_output`（`:701-703`）、`list_history`（`:735-737`）、`_history_path`（`:770-775`）、`open_output_dir`（`:965`）。设置项白名单 `DEFAULT_SETTINGS`（`:46-56`）+ `get_settings()`（`:158-173`）+ `set_settings()` 的 `if k in DEFAULT_SETTINGS` 过滤（`:176`）。
- **设计要点**：`DEFAULT_SETTINGS` 加 `outDir`（空=默认）+ `outNameTemplate`（如 `{stem}` / `{stem}-{date}`）；`_save_output`/`list_history`/`_history_path`/`open_output_dir` 统一走一个 `self._out_dir()`；可选 Obsidian front-matter 开关（`---\ntitle: …\nsource: …\n---`）。
- **最小实现路径**：1 个后端辅助方法 + 5 处替换 + 设置页 1 行 input（`index.html:200` 附近）+ `app.js` 绑定（`:755,784` 附近）+ 4 个 locale key。
- **验收标准**：设置成笔记库目录后新结果直接落在那里、历史列表读的是同一个目录、删除/打开目录行为一致；目录不可写时明确报错并回退默认；模板变量缺失时用文件名兜底。
- **工作量依据**：1 个后端文件 + 1 个前端文件 + locale；无依赖。**1–1.5 人日**。
- **风险**：历史/删除/回收站涉及用户数据，改动要保证"只影响新结果"；过程中 `OUTPUT_DIR` 若被别处 import（`webview_backend` 之外）需一并检查。

### E20 历史带缩略图 — 可行但低优先

- **现状证据**：`list_history` 只扫 `.md/.txt` 并返回 `{name,path,mtime,size}`（`webview_backend.py:735-745`）；源图不保留——inputs 是上传临时文件（`webview_server.py:431-470` 用完即删），PDF 页图也删（`ocr_engine.py:434-440`）。
- **设计要点**：落盘时顺带写一张首图缩略图（Pillow `thumbnail((160,160))` → `outputs/.thumbs/<name>.png`），`list_history` 返回 `thumb` 字段；前端列表项 `<img>`。
- **最小实现路径**：`_save_output` 增加 source 入参（现在没有，需要扩展契约）+ 缩略图生成 ~30 行 + `list_history` 加字段 + `app.js:502` 模板加 `<img>` + CSS。
- **验收标准**：历史项左侧出现缩略图；删除时连缩略图一起删（进回收站）；`_assets`/`.thumbs` 不被当历史项列出。
- **工作量依据**：2 个后端点 + 1 前端 + CSS；无依赖。**1.5–2.5 人日**。
- **风险**：磁盘增长（每结果一张图）与隐私（图片落到 outputs）；`_save_output` 契约变更会牵动两处调用（`:527,633`）。

---

## 3. 明确不建议做（附理由）

1. **真·置信度 / 每块质量分**：模型不输出 logprobs（§0.2）。要拿到只能 patch 下载目录里的 `modeling_unlimitedocr.py` 调用链（`_uocr_page_count_guard` 钩子），且需把 token 概率对齐回文本块；换模型版本即失效，误报还会让用户误信。**替代**：E3 的"截断/页数"是唯一可靠的质量信号。
2. **批量任务队列持久化（关掉程序不丢）**：现有后端是单任务状态机（`webview_backend.py:117-124,467-470`），做持久化队列要引入 job 模型、状态恢复、与取消/单任务互斥的交互，还要处理"恢复后继续跑会不会重复写 outputs / 重复烧 CPU"。**4–6 人日**换来的收益，远不如 E5+E16（目录扫描 + 失败重试）组合；建议推迟到有真实需求。
3. **文件夹直接拖入（drag-folder）**：Chromium 沙箱拿不到绝对路径（`DataTransfer` 只给 `File`），要递归 `webkitGetAsEntry` 再逐个上传，等于把 E5 的"免上传"优势抹掉，还要处理上万文件的上传。**用 E5 的目录选择器替代**。
4. **"把 Markdown 再喂给模型要求改成 JSON/其它格式"**：`infer` 在不传图、只传 prompt 时能跑（`modeling:1066-1067`），但模型是 OCR 训练目标，指令遵循弱、且 3.3B MoE 在 CPU 上极慢——结构化输出用规则（E2）比再生成一次可靠得多、也便宜两个数量级。
5. **多后端 / 多 GPU / 模型热切换**：不属于"拓功能"，属于架构替换；且本仓的 6.3GB 权重 + 单引擎锁（`ocr_engine.py:465`）已经是硬约束，换引擎等于重写 `ocr_engine.py`。**除非同时提供等价权重的开源替代，否则不碰。**
6. **图像预处理增强（去噪/纠偏/二值化）**：要引入 opencv（~60MB，与"安装包不含重依赖"的发行策略冲突，`README:28-32`），而模型自身有 1024 letterbox + gundam 切片预处理（`modeling:531-566`），没有证据表明能稳定提分。**先做 E2 采样看失败样本类型再决定。**
7. **结果 diff / 版本历史 / 多人协作**：与"本地单人小工具"定位不符，且 `outputs/` 是扁平文件目录（`webview_backend.py:735-745`），没有版本模型可依。

---

## 4. 依赖关系与建议落地顺序

```
E3(3–5人时) ─┬─> E14 表格校验（复用 warnings 通道）
             └─> 前端 banner
E2 JSON(2–3人日) ─┬─> E15 裁剪落盘
                  └─> E8 可搜索 PDF
E1 CLI(2–3人日) ── CLI 落地后 E3/E4/E5/E7 的开关顺带有命令行入口
E5 目录扫描(1–1.5人日) ── 与 E18 的 /api/recognize-paths 同源
```

建议节奏：**第一批（约 1 周）** E3 + E11 + E16 + E5 + E4 —— 全是低风险、不动模型、直接改善"每天用"的体验；**第二批** E1 + E2（CLI 与结构化输出，打开自动化/下游）；**第三批** E9/E10/E19（格式与集成）；**其余按需求触发**。E8/E20 需要先 spike 再承诺。

---

*本文档为只读探查产物。所有"实测"结论可用 `D:\Project\_work\probe_postproc.py`（bbox/页/层级解析、DROP_LABELS）与 `D:\Project\_work\probe_exports.py`（docx/xlsx 原型）复现；未在无 GPU 环境验证项已显式标注 [需 spike]。*
