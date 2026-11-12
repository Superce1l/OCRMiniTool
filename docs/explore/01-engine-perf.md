# 01 · 推理引擎与后处理：性能 / 资源 / 能力边界

> 探查范围：`ocr_engine.py`、`ocr_postprocess.py`、`models/Unlimited-OCR-2/*.py|json`（只读）。
> 版本 v1.2.0，HEAD `ecd717a`。分支上 20 个文件显示"全量修改"是 CRLF 假象，本文引用的是工作区实际内容（与 index 的差异仅行尾）。
> 机器：i7-10700（8C/16T，无 AVX512-BF16）、16 GB RAM、Windows 10；无 GPU 可用、无真实推理（`torch` 组件仅本机装过、本次未运行）。
> 标注约定：**【实测】**=本次跑了脚本（纯 Python/NumPy）或来自 `logs/` 的真实运行记录；**【代码推理】**=读代码+参数推算；**【通用知识】**=上游文档/经验。凡"更快/更省"的判断都带标注。

---

## 1. 摘要（按影响排序）

| # | 结论 | 依据等级 |
|---|------|----------|
| S1 | CPU 上**解码是内存带宽瓶颈**：每 token 需读 ~1.0–1.15 GB 权重（MoE 6/64 + lm_head 331 MB），本机实测带宽 8–12 GB/s ⇒ 理论上限 ~10–12 tok/s；且长文档还要叠加 KV 上采样开销（见 S2）。 | 实测+代码推理 |
| S2 | 解码每 token 会把**整份 KV 缓存 `.float()` 两遍**（`modeling_deepseekv2.py:475-476`），流量 ∝ 序列长度：20 页合并时额外 ~1.2 GB/token ⇒ 每 token 总流量 ~2.4 GB ⇒ ~5 tok/s。**合并模式的页数直接决定每 token 速度**。 | 代码推理（高置信） |
| S3 | 单页总时长被**视觉编码器**主导：本机 `_smoke_p1` 单页（输出仅 63 字）实测 **108 s**，按粗算其中 ~90% 是 SAM+CLIP 前向 ⇒ 有效算力仅 ~15 GFLOPS，而本机 fp32 GEMM 实测 **496 GFLOPS**（30x 差距）⇒ bf16-on-CPU 极可能走仿真路径。 | 日志实测+粗算推断 |
| S4 | `max_new_tokens` 默认 = `131072 - prompt_len`（`modeling_unlimitedocr.py:1039,1089`），后端从不传值（`webview_backend.py:521`）⇒ **单请求最坏可生成 13 万 token**，CPU 上 ~4 小时起步，且无看门狗。 | 代码推理 |
| S5 | `crop_mode`/`max_new_tokens` 在引擎里已打通（`ocr_engine.py:474-477`）但后端两个分支都没用（`webview_backend.py:521,630`）⇒ 最大的一档速度/精度开关对用户不可见。 | 事实 |
| S6 | 取消只在**解码**每 token 生效（`ocr_engine.py:70-81`），视觉+prefill 阶段完全不可中断（单页可达 ~100 s）；README 第 27 行"取消 1~2 秒内生效"与代码不符。 | 代码推理 |
| S7 | 后处理不是瓶颈：**【实测】**100 页 / 235 KB 原始输出 `convert()` 仅 **60 ms**（0.6 ms/页），且无正则回溯爆炸。不建议优化。 | 实测 |
| S8 | PDF 路径把每页 200 dpi PNG **落盘再读回**（`ocr_engine.py:365-366` vs `modeling_unlimitedocr.py:1070`），且全部页 PIL 同时驻留（11.6 MB/页）：100 页 ≈ **1.16 GB** 常驻 + 临时盘 25–216 MB。 | 实测+代码推理 |

---

## 2. 推理链路与时间去向

### 2.1 阶段表

| 阶段 | 代码位置 | 成本驱动 | 本机量级（单页 / 20 页合并） | 可优化性 |
|------|----------|----------|------------------------------|----------|
| 组件/模型加载 | `ocr_engine.py:285-347` | 6.69 GB safetensors → RAM | **实测** 6–21 s（`logs/app-202611.log:170`） | 低（已够快） |
| PDF→PNG | `ocr_engine.py:350-370` | 200 dpi 光栅化 + PNG 编码 | **实测代理** 65–195 ms/页 + 0.24–4.2 MB/页 | 中（可跳过落盘） |
| 读图转 RGB | `modeling_unlimitedocr.py:1070-1071` | 每页 PIL → RGB，全部常驻 | **实测** 12.9 ms/页 + 11.6 MB/页 | 中（改 JPEG/mmap） |
| 视觉编码 | `modeling_unlimitedocr.py:707-791` | SAM(1024²)+CLIP(基础视图+每个 gundam tile) | **粗算** 1.5–1.7 TFLOP/页 ⇒ ~100 s | **高**（tile 数可减） |
| LLM prefill | `modeling_unlimitedocr.py:1162-1164` | 激活 34 M 参数/token；专家权重 5.3 GB 读一遍 | 粗算 2–4 s/页 | 低 |
| decode | `modeling_unlimitedocr.py:1166-1195` | 权重 1.15 GB/token + KV 上采样 | 粗算 0.1–0.2 s/token ⇒ 5–10 tok/s | 中（限页/限 token） |
| 后处理 | `ocr_postprocess.py:322-353` | 纯正则/字符串 | **实测** 0.6 ms/页 | 无 |

### 2.2 每 token 成本模型（代码推理，可复核）

MoE 每 token 只激活 6/64 专家（`config.json` `num_experts_per_tok:6`），`moe_infer` 只对非空专家做 GEMM（`_sparse_base_impl.py:793-800`）：

| 项 | 每 token 字节 | 说明 |
|----|---------------|------|
| 路由专家 6×12 层 | 6×3×(1280×896)×2 B×12 ≈ 496 MB | `_sparse_base_impl.py:793-800` |
| 共享专家 2×12 层 | ≈ 166 MB | `_sparse_base_impl.py:746-747` |
| 注意力权重 4×12 层 | ≈ 157 MB | 10 头×128 维 |
| **lm_head（129280×1280）** | **331 MB** | `modeling_unlimitedocr.py:1167` |
| KV `.float()` 上采样 | 122,880×S 字节 | `modeling_deepseekv2.py:475-476`（S=5500 时 676 MB） |
| KV 索引 `.cat` | ≈ 17 MB | `modeling_deepseekv2.py:515-516` |

⇒ 单页（S≈500）约 1.15 GB/token；实测带宽 8–12 GB/s ⇒ **~90–140 ms/token**。20 页合并（解码中 S≈1 万）约 2.4 GB/token ⇒ **~200–300 ms/token**。
**复核方法**：`return_dict=True` 取 `time`/`prefill_time` 与 `len(token_ids)` 相除即得实测 TPS（字段已存在，见 F1）。

### 2.3 视觉编码器的 tile 成本（代码推理）

`dynamic_preprocess` 按长宽比选 tile 网格（`modeling_unlimitedocr.py:229-267`，`min_num=2,max_num=16`），基础视图恒为 1024×1024（`modeling_unlimitedocr.py:561`）。每页图像 token（`per_page_image_tokens=273`，`config.json:161`）：

| 长宽比 | tile 网格 | 图像 token/页 | 相对视觉成本 |
|--------|-----------|---------------|--------------|
| 正方 | 1×1（≤640 图直接单块） | 273 | 1.0× |
| A4 竖 0.71 | 1×2 | 493 | ~1.4× |
| 16:9 横 1.78 | 2×1 | 493 | ~1.4× |
| 4:3 横 | 2×2 | 693 | ~2.0× |
| 长截图 1:8 | 1×8 | 1153 | ~4.4× |
| 极长截图 1:16 | 1×16 | 2033 | ~8.4× |

（token 公式：`273 + (10·w+1)·10·h`，`nq=10`，见 `modeling_unlimitedocr.py:546,564-567`。视觉成本≈(tile 数+1)×基础视图，粗算。）

---

## 3. 主要发现

### F1｜诊断信息被丢弃：拿不到 prefill/decode 拆分和"是否截断" —【事实】
- 证据：`infer(..., return_dict=True)` 会返回 `prefill_time / time / finish_reason / token_ids / num_pages_out`（`modeling_unlimitedocr.py:1202-1205`），引擎调用时不传 `return_dict` 且把返回值当字符串（`ocr_engine.py:479-480`）。
- 影响：无法回答"时间花在视觉还是解码"，也无法发现 `finish_reason='length'`（输出被 131072 或 `max_page_tokens` 截断）——用户只会看到"结果莫名少了几段"。
- 建议：`_infer` 改传 `return_dict=True`，把 `prefill_time/time/len(token_ids)/finish_reason/expected_pages` 写 `log_ocr`，并在 `finish_reason!='stop'` 时给 UI 提示"输出可能被截断"。
- 收益：零功能风险，是所有后续优化的度量前提；能把 F2/F3 的收益直接量化。风险：低。
- 验证：日志出现 `prefill=…s decode=…s tps=… finish=…`。

### F2｜生成上限过大且不可配 —【事实+代码推理】
- 证据：默认 `max_length=131072`、`max_new_tokens=None` ⇒ `max(1, 131072-prompt)`（`modeling_unlimitedocr.py:1040,1089`）；页数守卫只提供"每页 20000 token"的软上限（`modeling_unlimitedocr.py:1042,1170-1172`、`583-605`）。
- 影响：一个跑飞的请求在 CPU 上可占用数小时；`max_page_tokens=20000` 本身也远超单页实际需求（一页 ~3000 字 ≈ 2500 token）。
- 建议：设置项 `单页最大生成 token`（默认 4096，范围 1024–20000）→ 映射到 `max_page_tokens`；另加全局 `max_new_tokens` 上限 = `页数×单页上限`。**不要**只暴露裸 `max_new_tokens`（用户会设成 1 导致全截断）。
- 收益：最坏耗时从 ~131k token 降到 ≤ `页数×4096`（20 页：4 小时 → ~25 分钟，按 5–10 tok/s）。风险：中（长表格页可能被截断，必须配 F1 的提示）。验证：构造超长表格页，看 `finish_reason` 与字数。

### F3｜合并模式无页数上限，且页数直接拖慢每 token —【代码推理】
- 证据：`_infer(expanded, ...)` 一次塞入全部页（`ocr_engine.py:425-433`）；解码每 token 上采样整份 KV（`modeling_deepseekv2.py:475-476`）；KV 大小 60 KB/token（12 层×2×10 头×128 维×2 B）。
- 影响：① 每 token 时间随页数线性增长（20 页约 2 倍于单页）；② SDPA 在 CPU 走 math 后端，prefill 每个 query chunk 需 `[1,1,2048,S]` 掩码 + 内部 `[10,2048,S]` 分数（S=25k 时可达 GB 级瞬时峰值，**推断**）；③ 取消要等整段 prefill。
- 建议：`设置 → 合并页数上限`（默认 30），超限自动分组 + 组间检查取消 + 结果拼接；`outputs` 里已有 `<!-- page N -->` 结构，拼接成本低。
- 收益：单请求峰值内存降到可控；大文档总时长基本不变（视觉成本不变），但可取消、可反馈。风险：中（分组边界处丢失跨页上下文，跨页表格可能断）。验证：50 页 PDF A/B，看峰值 RSS（任务管理器）与 `finish_reason`。

### F4｜取消语义：只覆盖解码，不覆盖视觉/prefill —【事实】
- 证据：取消检查只在 `on_finalized_text`（`ocr_engine.py:72-80`，由 `modeling_unlimitedocr.py:1184` 的 `streamer.put` 驱动）；prefill 在 `1162-1164`，视觉在 `707-791`，二者之间没有任何检查点。引擎文档串却写"单次大请求内不可中断"（`ocr_engine.py:17`），README 写"1~2 秒内生效"（`README.md:27`）。
- 影响：点取消后，单页最坏 ~100 s、20 页最坏数分钟无响应；用户会重复点或强杀进程（强杀会留下 `_tempfile` 目录）。
- 建议：① 修正 README/docstring 为"取消在下一个 token 生效；图片/PDF 的视觉编码阶段（可达数十秒）无法中断"；② batch 模式已有页间检查点（`webview_backend.py:603`），合并模式按 F3 分组后组间检查；③ UI 上把"取消中…（等待当前前向结束）"作为独立状态。
- 收益：消除预期落差，减少强杀；在 20 页场景把最坏响应从分钟级降到"组大小×prefill"。风险：低（纯提示+分组）。验证：识别 20 页 PDF 时点取消，记录到真正停止的秒数。

### F5｜取消异常会在 `finally` 里二次抛出，可能掩盖真实错误 —【事实】
- 证据：`streamer.end()` 也走 `on_finalized_text(text='', stream_end=True)`（`transformers/generation/streamers.py:119-132`），钩子不看 `stream_end` 就再抛一次（`ocr_engine.py:76-80`）；调用点在 `modeling_unlimitedocr.py:1196-1200` 的 `finally` 中。
- 影响：若推理先抛了真实异常（OOM/shape/断言）而用户同时也点了取消，最终上报的是"已取消"，日志里 `_error` 也被覆盖（`ocr_engine.py:485-488`）——排障时会丢掉真因。
- 建议：钩子在 `stream_end=True` 时只计数不抛（取消由 `recognize()` 末尾的兜底判定 `ocr_engine.py:446-447` 负责），并让 `except OcrCancelledError` 分支先记 `log_error` 再转换。
- 收益：保留真因，减少"取消掩盖 OOM"类误判。风险：低。验证：人为抛异常 + 置位 cancel_event，确认日志两条都在。

### F6｜bf16-on-CPU 是最大嫌疑（需 A/B） —【实测+推断】
- 证据：本机无 AVX512-BF16（i7-10700，Comet Lake）；模型全程 bf16（`ocr_engine.py:329`）。实测锚点：**单页 108 s、输出 63 字**（`logs/app-202611.log:172`、`outputs/_smoke_p1.md`）⇒ 时间几乎全在视觉前向；而同机 fp32 GEMM **实测 496 GFLOPS**（`numpy`+OpenBLAS，1280×896×1280，5.92 ms/次）。按视觉 ~1.5 TFLOP 粗算，有效算力仅 ~15 GFLOPS。
- 影响：若成立，CPU 用户拿到的是 3–10 倍的额外耗时；这是"CPU 版慢"的主因，而非 MoE/模型本身。
- 建议（低成本试探，不改模型代码）：加载后把**视觉塔**转 fp32：`model.model.sam_model.float(); model.model.vision_model.float(); model.model.projector.float()`（参数约 392 M，bf16→fp32 仅 +0.78 GB，精度影响可忽略）。**不要**整体转 fp32（6.69→13.4 GB，16 GB 机器会换页）。
- 收益：若推断正确，单页 prefill 可降 1.5–3 倍（100 s → 35–65 s）；若无效，回退是一行。风险：中（RAM +0.8 GB；需实测确认，不能凭猜）。验证：同一张图 bf16 vs 视觉塔 fp32，各跑 3 次取中位；同时记录 `prefill_time`（F1）。

### F7｜gundam 默认策略让 CPU 用户承受最贵的一档 —【事实】
- 证据：`crop_mode=None` ⇒ 单图自动 gundam（`modeling_unlimitedocr.py:541`），引擎单图分支不覆盖（`ocr_engine.py:419`）；batch 模式每个文件都走这条（`webview_backend.py:630`）。
- 影响：A4/16:9 页 = 2 tile（视觉 ~1.4×）、4:3 = 4 tile（~2×）、长截图可达 16 tile（~8.4×，单张 CPU 上可到 5–10 分钟）。
- 建议：设置项 `版面切片`：`自动(默认)` / `精细 gundam` / `快速 base`；CPU 设备在"自动"时对**长截图（tile≥8）**降级为 base 并提示。`crop_mode` 透传已就绪（`ocr_engine.py:474-475`）。
- 收益：长截图 CPU 上 ~2–5×；普通 A4 约 1.4×。风险：中（密集小字页精度会降，必须让用户可选且记日志）。验证：同一张长截图两种模式对比耗时+输出 diff。

### F8｜资源：6.69 GB 权重 + KV + 全页 PIL，16 GB 机器余量很小 —【实测+代码推理】
- 证据：`model.safetensors.index.json` 的 `total_size=6,687,958,536`（6.69 GB，2819 tensor）；全页 PIL 常驻（`modeling_unlimitedocr.py:1070-1071`，11.6 MB/页@200dpi，**实测**）；KV 60 KB/token；`unload()` 无任何入口（`ocr_engine.py:495-500` 是死代码，UI 只弹提示 `webview/js/app.js:633-636`，无 `/api/unload` 路由）。
- 影响：100 页 PDF ≈ 1.16 GB PIL + 6.69 GB 权重 + torch ~1 GB + KV 0.6 GB ≈ **9.5 GB**，接近 16 GB 上限；`start_load` 不检查 `ready`（`webview_backend.py:301-311`，仅 UI 禁用按钮 `webview/js/app.js:603`），直接调 API 会加载第二份 6.69 GB。
- 建议：① `load()` 开头 `if self.ready and self.device==target: return` 或先 `unload()`；② 加 `/api/unload` 接上已有 `engine.unload()`（切设备不必重启）；③ PDF 解码改为"逐页打开→算完即释放"（需改 `infer` 接口，见 A8 的替代方案）。
- 收益：防 OOM 崩溃（一次事故 = 全部结果丢失），并让"切设备/换模型目录"免重启。风险：低。验证：`get_status` 前后对比进程私有内存；连续两次 `/api/load` 观察 RSS。

### F9｜PDF 路径：dpi 固定 200、落盘再读、临时目录在系统盘 —【事实+实测】
- 证据：`dpi=200` 为形参默认值且调用处不传（`ocr_engine.py:351,410`）；PNG 写到 `tempfile.mkdtemp(prefix="ocr_pdf_")`（`ocr_engine.py:408`，即 `%LOCALAPPDATA%\Temp`），`finally` 里 `rmtree`（`434-440`）；随后 `infer` 再 `Image.open(f).convert('RGB')`（`modeling_unlimitedocr.py:1070`）。
- 实测（Pillow 代理，A4@200dpi = 1654×2339）：文字页 PNG 编码 65 ms/帧、扫描页 195 ms/帧/4.2 MB；解码 7–8 ms；`open+convert` 12.9 ms/页。
- 影响：① 相对 40–100 s/页的推理，编解码 <1%（**不是**时间瓶颈）；② 但 100 页扫描件会在系统盘留 200–400 MB 临时文件，进程被强杀则残留；③ dpi 下限：模型 tile 目标最大 1280 px（A4 竖 1×2），200 dpi 的 1654 px 已有余量；**若走 base 模式（1024×1024）**，150 dpi（1240 px）就够。
- 建议：① dpi 暴露为 `标准 200 / 快速 150`，与 F7 联动（base 模式下自动 150）；② 直接把手上的 `fitz.Pixmap` 转 PIL 传给 `model.infer(image_file=[PIL.Image])`（模型端已支持 PIL，`modeling_unlimitedocr.py:1070`）——省掉落盘+回读，且天然消除临时目录；③ 启动时清理上次遗留的 `ocr_pdf_*`。
- 收益：PDF 路径省 ~0.2 s/页 + 全额临时盘（100 页 ≈ 200–400 MB），并修掉强杀残留。风险：低（接口在模型侧已支持）。验证：100 页 PDF 识别前后 `%TEMP%` 占用、`open+convert` 时间。

### F10｜后处理已足够快，不要动它 —【实测】
- 证据/数据：见 §6 附录表。100 页/235 KB → `convert()` 60 ms；`to_gui_markdown()` 与之几乎相同；单条正则最贵是 `BARE_BLOCK_RE`（2.2 ms/20 页）。恶意输入下最坏 37 ms（4 万字符病态 bbox），无回溯爆炸。
- 影响：占比 <0.1%，任何"优化"都是负收益（引入 bug 的风险远大于收益）。
- 建议：仅在 `convert()` 外包一层计时日志（>200 ms 时告警），不做重构。风险：低。验证：已有基准可复用（本文附录脚本逻辑）。

### F11｜可观测性缺口：状态轮询每次都跑一遍设备探测 —【事实】
- 证据：`get_status()` 每次返回都调 `self.engine.device_info()`（`webview_backend.py:220`），后者会 `import torch`、`torch.cuda.is_available()`、`get_device_name/properties`（`ocr_engine.py:250-266`）。
- 影响：CPU 机上开销小（~1 ms），但 GPU 机上与推理并发时会产生 CUDA 上下文交互；SSE 重连/每次 `_publish_status` 都会触发。
- 建议：device_info 结果缓存 5 s（`_loaded_device` 已经缓存了"实际加载设备"，硬件探测结果可同样缓存）。风险：低。验证：`/api/status` 连打 100 次的总耗时。

### F12｜"流式取消"依赖对模型模块的全局打补丁，脆弱但当前有效 —【事实】
- 证据：`load()` 遍历 `sys.modules` 找 `*modeling_unlimitedocr` 并替换模块级 `TextStreamer`（`ocr_engine.py:337-341`，找不到就静默不生效）；`_STREAM_HOOK` 是模块级可变 dict（`ocr_engine.py:62`），靠 `recognize()` 的 try/finally 置空（`398-399,441-443`）。
- 影响：① 若 transformers 动态模块名变化/多份副本，取消与字数统计**静默失效**（没有断言/日志）；② 全局 dict 让"同一进程两个引擎实例"不可能，当前靠 `recognizing` 单飞保证（`webview_backend.py:467-470`），一旦以后加"并行识别"就是竞态。
- 建议：① 注入后 `assert` 打补丁成功（找不到模块时 `log_error`，不要静默）；② 在 `stream_state` 记录一次"hook 已生效"标记，`get_status` 暴露给开发日志；③ 顺带把取消改成线程局部/实例属性（`self._stream_hook`）以解除全局约束。
- 收益：把"取消失效"从静默变成可见；为未来并行留路。风险：低。验证：断点/日志确认 `TextStreamer` 已被替换；模拟模块改名后应看到告警。

---

## 4. 参数暴露矩阵（"没暴露/没被用"的完整清单）

| 参数 | 现状 | 默认值 | 影响 | 建议 | 风险 |
|------|------|--------|------|------|------|
| `max_new_tokens` | 引擎支持，后端从不传（`ocr_engine.py:476`；`webview_backend.py:521,630`） | `131072-prompt` | 最坏时长 | 改为"单页 token 上限"设置（见 F2） | 中 |
| `crop_mode` | 引擎支持，后端从不传（`ocr_engine.py:474`） | `None`⇒单图 gundam | 视觉成本 1.4–8.4× | 设置项三档（见 F7） | 中 |
| `page_count_guard` / `max_page_tokens` | 不可达 | 开 / 20000 | 输出完整性、最坏时长 | 随 F2 暴露 token 上限，guard 保持开 | 低 |
| `max_crops` | 不可达（`modeling_unlimitedocr.py:1043`） | 16 | 长截图 tile 上限 | 不单独暴露；由 F7 的 base 降级覆盖 | 中 |
| `temperature` | 硬编码 0.0（`ocr_engine.py:471`） | 贪心 | 确定性 | **保持不暴露**（OCR 需可复现） | — |
| `no_repeat_ngram_size` | 不可达 | 0（关） | 长表格重复文本 | 不进 UI；如遇退化再做内部实验 | 中 |
| `dash_topm` / `dash_topk_blocks` | 不可达 | 16 / 12 | 精度-速度权衡 | 不动（精度契约） | 高 |
| `dpi` | 写死 200（`ocr_engine.py:351`） | 200 | PDF 清晰度/耗时 | 两档设置（见 F9） | 低 |
| dtype | 写死 bf16（`ocr_engine.py:329`） | bf16 | CPU 速度 | 仅做 F6 的"视觉塔 fp32"内部 A/B | 中 |
| `torch` 线程数 | 从不设置 | 物理/逻辑核 | CPU 吞吐 | 可做 A/B（`set_num_threads(8)`），收益不确定 | 低 |
| `attn_implementation` | 未传 | `eager`→`mha_eager`（`_sparse_base_impl.py:3639-3641`） | 无实际开关 | **不建议**碰（见 §7） | 高 |
| `return_dict` | 不传 | False | 诊断（F1） | 传 True 并记日志 | 低 |

---

## 5. 取消语义（矩阵 + 竞态）

| 阶段 | 可取消？ | 检查点 | 最坏等待（本机粗算） |
|------|----------|--------|----------------------|
| 队列/忙判定 | ✅ | `webview_backend.py:467-470` | 立即 |
| PDF 转图 | ❌ | 无（`ocr_engine.py:402-412`） | ~0.2 s/页 |
| 视觉编码 | ❌ | 无（`modeling_unlimitedocr.py:707-791`） | 单页 ~60–100 s；16 tile 长截图可达数分钟 |
| LLM prefill | ❌ | 无（`1162-1164`） | 单页 2–4 s；20 页 30–60 s |
| decode | ✅ | 每 token（`1184`→`ocr_engine.py:76-80`） | ≤1 token，~0.1–0.3 s |
| batch 页间 | ✅ | `webview_backend.py:603,645` | 当前页结束时 |

竞态/边界清单：

| # | 现象 | 证据 | 级别 |
|---|------|------|------|
| C1 | 取消事件在 `recognize()` 开头被 `clear()`，紧邻请求的取消可能丢失 | `webview_backend.py:478` | 低（毫秒窗口） |
| C2 | `finally` 里二次抛取消，掩盖真因 | F5 | 中 |
| C3 | 全局 `_STREAM_HOOK` 与"单飞"假设绑定 | F12 | 低（当前）/中（未来并行） |
| C4 | 打补丁失败时静默（无取消、无字数） | `ocr_engine.py:338-341` | 中 |
| C5 | 取消后临时目录靠 `finally` 清理；强杀残留 | `ocr_engine.py:434-440` | 低 |
| C6 | `_infer` 持 `self._lock`（`ocr_engine.py:465`），若将来加 `unload` 会阻塞到推理结束 | — | 低 |

---

## 6. 后处理实测数据（附录）

方法与脚本：`C:/Users/admin/AppData/Local/Temp/bench_post.py`，runtime python 3.12（numpy/Pillow，无 torch），`time.perf_counter` 单次（非取平均；量级足够）。合成输入按真实 token 形态（title/LEVEL/text/cross_table/append_table/image + bbox）构造。

| 页数 | 原始体积 | `convert()` | `to_gui_markdown()` | 其中 parse / groups / render |
|------|----------|-------------|---------------------|------------------------------|
| 1 | 2 KB | 1.10 ms | 0.67 ms | 0.27 / 0.04 / 0.17 |
| 5 | 12 KB | 3.05 ms | 3.03 ms | 1.30 / 0.14 / 0.75 |
| 20 | 47 KB | 13.00 ms | 11.94 ms | 5.28 / 0.57 / 2.96 |
| 50 | 118 KB | 30.47 ms | 30.00 ms | 12.94 / 1.47 / 7.38 |
| 100 | 235 KB | 60.15 ms | 60.21 ms | 25.83 / 3.05 / 14.59 |

单正则（20 页/47 KB）：`PAGE_SPLIT_RE` 0.24 ms、`BLOCK_RE` 0.92 ms、`BARE_BLOCK_RE` 2.24 ms、`STRAY_TOKEN_RE` 1.36 ms。
病态输入（最坏 37 ms）：4 万字符未闭合 bbox 列表（`BLOCK_RE` 37 ms）、500 KB 无块正文（`BARE_BLOCK_RE` 22 ms）——**无回溯爆炸**，均有线性上界。
表合并：3000 行 × 3 片段 14 ms（`_merge_table_fragments`，`ocr_postprocess.py:226-249`）。

---

## 7. 低风险可落地清单（供 Lead 决策）

| 编号 | 改动点（文件:行） | 收益 | 风险 | 验证 |
|------|--------------------|------|------|------|
| A1 | `_infer` 传 `return_dict=True`，记录 `prefill_time/time/token 数/finish_reason`（`ocr_engine.py:479`） | 一切优化的度量前提；可发现输出截断 | 低 | 日志出现 prefill/decode/tps |
| A2 | 新增"单页最大生成 token"设置（默认 4096）→ `max_page_tokens`（`ocr_engine.py:476`，`modeling_unlimitedocr.py:1042`） | 最坏时长 4 h → ~25 min（20 页） | 中（长页截断，需 A1 提示） | 超长表格页看 `finish_reason` |
| A3 | 合并模式页数上限 + 自动分组 + 组间取消检查（`ocr_engine.py:425-433`） | 消 GB 级 prefill 峰值、取消延迟从"分钟"到"组"；大文档可预期 | 中（跨组上下文丢失） | 50 页 PDF 峰值 RSS + 取消时延 |
| A4 | 修正 README:27 与 `ocr_engine.py:17` 的取消描述；UI 增加"取消中（等待当前前向）" | 消除"点了没反应"的差评源 | 低 | 点取消后 UI 文案/时延 |
| A5 | 钩子在 `stream_end=True` 时不再抛，只在解码中抛（`ocr_engine.py:72-80`） | 保留真实错误（OOM/断言）不被"已取消"掩盖 | 低 | 注入异常 + 取消，日志两条都在 |
| A6 | `crop_mode` 三档设置 + 长截图自动降级 base（`ocr_engine.py:419`，`webview_backend.py:521`） | 长截图 CPU 2–5×；A4 约 1.4× | 中（小字精度） | 同图两模式耗时 + 文本 diff |
| A7 | `dpi` 两档（200/150）并与 base 模式联动（`ocr_engine.py:351,410`） | PDF 转图时间/临时盘减 30–45% | 低 | 100 页 PDF 前后耗时/`%TEMP%` |
| A8 | PDF 直接传 PIL 给 `model.infer`，不再落盘 PNG（`ocr_engine.py:350-370`） | 省 ~0.2 s/页 + 全部临时盘（200–400 MB/100 页），消除强杀残留 | 低（模型侧已支持 PIL） | 100 页 PDF 前后 `%TEMP%` 与总耗时 |
| A9 | 上传/页数护栏：PDF > N 页（如 100）或 > X MB 时提示确认（`webview_server.py:431-470`） | 防 1.16 GB PIL + 数小时占用导致的 OOM | 低 | 上传 200 页 PDF 应被拦 |
| A10 | `load()` 幂等化（ready 且同设备直接返回）+ 新增 `/api/unload` 接 `engine.unload()`（`ocr_engine.py:285,495`；`webview_backend.py:301`） | 防 2×6.69 GB 双载；切设备免重启 | 低 | 连续两次 `/api/load` 的 RSS |
| A11 | `device_info` 结果缓存 5 s（`webview_backend.py:220`） | 去掉每次状态轮询的 torch/CUDA 探测 | 低 | `/api/status` ×100 耗时 |
| A12 | 视觉塔 fp32 A/B（`ocr_engine.py:330` 后加一行；见 F6） | 若成立，CPU prefill 1.5–3× | 中（需实测，RAM +0.8 GB） | 同图 3 次取中位 + `prefill_time` |

---

## 8. 不建议做（及理由）

| 项 | 理由 |
|----|------|
| `torch.compile` | Windows CPU 需 MSVC/clang 工具链；模型有 q_len=1 / q_len=S 两种形状 + 自定义 DASH/环缓冲/动态 mask，编译期与 recompile 开销很可能超过收益；打包发布不可控。 |
| 强制 `attn_implementation="flash_attention_2"/"sdpa"` | 该类走 `mha_*` 前缀（`_sparse_base_impl.py:3639-3641`），flash-attn 在 Windows CPU 无 wheel；`sdpa` 会绕过 dash 覆盖层（`modeling_deepseekv2.py:68-93`）。精度契约不可验证。 |
| 关闭 DASH 稀疏注意力 | 精度直接受损，且需要在模型代码里改 `_INFER`；`_uocr_generate` 每次都会重新打开（`modeling_unlimitedocr.py:1154`）。 |
| 量化（int8/4bit） | 无 VNNI 的 CPU 上 int8 反而不快（通用知识）；权重来自不可控的远程代码，量化后精度退化无法与上游对齐；改动面覆盖 MoE+视觉。 |
| 多线程并行多页 | 单模型 6.69 GB、CPU 内存带宽已饱和（实测 8–12 GB/s），并行只会更慢并破坏 `_STREAM_HOOK` 的全局单飞假设。 |
| 自实现 mmap/分片加载 | transformers 4.52 已移除 `low_cpu_mem_usage` 参数、按分片直载（`.venv/.../modeling_utils.py:4152`），实测加载 6–21 s，收益 ≤1%。 |
| 暴露 `temperature` 采样 | OCR 输出需确定性；采样会引入不可复现结果与重复/漏字。 |
| 优化 `ocr_postprocess` | 实测 0.6 ms/页（§6），占比 <0.1%；重构风险 > 收益。 |
| 把后处理移到后台线程/子进程 | 同上，且会引入跨进程序列化开销。 |

---

## 9. 待实测假设（本次环境无法验证）

| # | 假设 | 需要的条件 | 建议测法 |
|---|------|-----------|----------|
| H1 | bf16 在无 AVX512-BF16 的 CPU 上比 fp32 慢 2–4×（F6） | 可加载模型（GPU 机或 32 GB 机器） | 视觉塔 fp32 A/B；`prefill_time` 对比 |
| H2 | 合并 20 页 prefill 的 SDPA math 峰值达 0.4–2 GB（F3） | 同上 | 观察峰值 RSS，或 `torch.profiler` memory |
| H3 | GPU 上 20 页合并是否受 7 GB 显存限制（KV 60 KB/token + prefill 分数） | ≥8 GB NVIDIA | 20 页合并跑通 + `nvidia-smi` |
| H4 | 单页 108 s 中视觉:解码的真实比例 | 已有 `prefill_time` 字段（A1） | 先上 A1，跑 3 张图即可得 |
| H5 | `torch.set_num_threads(8)`（物理核）是否优于默认 16 线程 | 同 H1 | 2×3 次中位对比 |
