# 无头命令行（cli.py）

`cli.py` 让 OCRMiniTool 在没有图形界面时也能用：不开窗口、不起本机 HTTP server、
不 import pywebview/clr，直接把图片 / PDF 转成 Markdown、纯文本或结构化 JSON。

适用场景：脚本批处理、CI 把扫描件转成可 diff 的 Markdown、Agent 调用、
把识别接进自己的流水线。

> 实现说明：识别仍走 `ocr_engine.OcrEngine`（与 GUI 同一个引擎、同一份模型、
> 同一套设置），落盘的 md/txt 复用后端 `_md_to_plain` / `_sanitize_name`，
> 所以 CLI 与界面产出的文本口径一致。

---

## 1. 快速开始

```bash
# 单张图片 → 默认在当前目录生成 a.md
python cli.py --input a.png

# 目录（默认只扫一层）→ 每张图一份 md，输出到 out/
python cli.py --input ./scans --out ./out

# 递归扫描子目录
python cli.py --input ./scans --recursive --out ./out

# PDF 只识别第 1-3 页与第 10 页到末页，导出纯文本
python cli.py --input report.pdf --pages "1-3,10-" --format txt --out ./out

# 管道模式：stdout 只有一份 JSON，不落盘（进度/告警走 stderr）
python cli.py --input a.png --json | jq '.pages[0].markdown'

# 批量 + JSON（每份结果一个 .json 文件）
python cli.py --input ./scans --format json --out ./out

# 环境/模型状态（不加载模型，秒回）
python cli.py status
python cli.py status --json

# 版本
python cli.py --version
```

运行位置：在项目根目录执行 `python cli.py …`（或 `run.bat` 建好的 venv 里的
python）。不要 `cd` 到别的目录再调用相对路径的 `cli.py`——脚本仍能跑，但默认
输出目录会变成当时的当前目录。

---

## 2. 参数表

| 参数 | 说明 |
|---|---|
| `--input <文件或目录>` | 必填。目录默认只扫一层，`--recursive` 才递归 |
| `--recursive` | 目录输入时递归子目录 |
| `--prompt "<指令>"` | 识别指令（留空 = 模型默认的文档解析） |
| `--out <目录>` | 结果落盘目录，默认当前目录；不存在会自动创建 |
| `--format md\|txt\|json` | 每份结果的落盘格式，缺省 `md` |
| `--device auto\|cuda\|cpu` | 推理设备，默认 `auto`（有 NVIDIA 用 CUDA，否则 CPU） |
| `--max-page-tokens N` | 单页输出 token 上限。**引擎支持时透传，否则告警忽略**（见 §5） |
| `--max-merged-pages N` | 每次请求最多合并多少页。引擎支持时透传；否则 CLI 侧按 N 页分块 |
| `--crop-mode auto\|gundam\|base` | 切片策略。**缺省=沿用引擎旧行为**（单页 gundam、多页 base）；显式 `auto`=按长宽比自动（长截图 h/w>3 → base，其余 gundam）；`gundam`/`base`=强制 |
| `--drop-headers` | 丢弃页眉/页脚/页码块。**引擎支持时透传，否则告警忽略**（见 §5） |
| `--pages "1-3,5,10-"` | 仅对 PDF 生效的页范围（1-based；`10-` 表示到末页）。与 GUI 设置 `pdfPageRange` 同一份解析实现，但**只作用于本次运行**（见 §5.1） |
| `--json` | stdout 只输出一个 JSON 对象（进度/告警一律走 stderr） |
| `--quiet` | 抑制进度行与逐文件成功行（错误仍然打印） |
| `--verbose` | 失败时打印 traceback（默认只给一行人话错误） |
| `--lang zh\|en` | CLI 文案语言；也可用环境变量 `OCRMT_LANG`（默认 zh） |
| `--version` | 打印 `version.py` 的 `APP_VERSION` 后退出 |
| `status`（子命令） | 打印模型目录/模型文件/推理组件/torch/transformers/pymupdf 状态，**不加载模型** |

扩展名白名单与后端一致：`jpg jpeg png bmp webp tif tiff` + `pdf`。
`--pages` 语法：逗号分隔的「单页 / 闭区间 / 开区间」，越界夹取并去重
（例如 5 页 PDF 上 `1-3,5` → `[1,2,3]`，`10-` → `[5]`）。

---

## 3. 输出契约

### 3.1 落盘（`--format md|txt|json`）

文件名 = 输入文件名的 stem（非法字符净化、超长截断，同 GUI），后缀随格式；
**同名不覆盖**：`a.md` 已存在时依次写 `a (1).md`、`a (2).md`。

- `md`：与 GUI 的 Markdown 一致；多页时页间有 `<!-- page N -->` 分隔行
- `txt`：复用后端 `_md_to_plain`（去标题井号/粗斜体/链接/代码围栏，表格单元格转 tab）
- `json`：见下（每份结果一个 JSON 文件）

### 3.2 `--json` 的 stdout

**stdout 永远只有一个 JSON 对象**，可直接 `json.loads` / 管道消费：

```json
{
  "ok": true,
  "results": [
    {
      "source": "D:/scans/a.png",
      "pages": [{"page": 1, "markdown": "# 标题\n\n正文"}],
      "elapsed": 1.234,
      "truncated": false,
      "pagesMismatch": false,
      "groups": 1,
      "outPath": null,
      "error": null
    }
  ],
  "summary": {"total": 1, "ok": 1, "failed": 0, "elapsed": 1.234},
  "error": null,
  "source": "D:/scans/a.png",
  "pages": [{"page": 1, "markdown": "# 标题\n\n正文"}],
  "truncated": false,
  "pagesMismatch": false
}
```

- `results[]` 每项对应一个输入文件；失败项 `ok=false` 时 `error` 有文案、`pages` 为空。
- 只有**一个输入**时，`source/pages/elapsed/truncated/pagesMismatch/groups/outPath/error`
  会平铺到顶层（方便 `jq .pages`），`results` 仍在。
- `truncated`：模型被生成上限截断；`pagesMismatch`：模型出的页数少于期望页数
  （可能丢页）。二者来自引擎，**引擎/旧包装层没提供时恒为 `false`**，不会报错。
- `groups`：引擎实际分组数（`int`；未分组=1，CLI 侧分块时是各次之和；旧引擎缺该字段时为 `0`）。
- **`--json` 且没给 `--format` 时不落盘**（纯管道模式）；给了 `--format` 则既写文件
  也在 stdout 输出 JSON。

### 3.3 进度与日志

进度（加载模型、当前文件）、告警、错误一律走 **stderr**；`--json` 时 stdout
保持干净。`--quiet` 只抑制进度与成功行，不抑制错误。

---

## 4. 退出码

| 码 | 含义 | 典型原因 |
|---|---|---|
| **0** | 全部成功 | 正常结束 |
| **1** | 参数错误 | 缺 `--input`、路径不存在、扩展名不支持、`--pages` 语法错、`--out` 不可写、未知参数 |
| **2** | 环境 / 模型错误 | 模型文件不完整、未装 torch 推理组件、CUDA 组件不匹配、模型加载失败、PDF 输入缺 pymupdf |
| **3** | 有文件失败 | 单个文件识别异常（其余文件继续），**全部失败也返回 3** |
| **130** | 被中断 | 运行时 Ctrl+C（`engine.cancel_event` 会置位） |

`--help` / `--version` 返回 0。参数解析失败统一归到 **1**（不是 argparse 默认的 2），
因为本 CLI 的 2 专指「环境/模型」问题。

---

## 5. 与引擎能力的关系（重要）

CLI 用 `inspect.signature(engine.recognize)` **探测参数名**，因此既吃新引擎的
能力，也能在旧引擎上优雅降级——不硬依赖、不假装支持：

| 参数 | 新引擎（已支持） | 旧引擎（未支持） |
|---|---|---|
| `--max-page-tokens` | 透传 `max_page_tokens=N`（`0`=不限；引擎会夹到 `[256, 65536]` 与上下文上限） | stderr 告警「已忽略」，继续识别 |
| `--max-merged-pages` | 透传 `max_merged_pages=N`，引擎在**进入推理前**按 N 页切组（组间可取消；跨组不共享上下文） | **CLI 自己分块**：按 N 页一组多次调用 `engine.recognize`，结果按页序拼回 |
| `--drop-headers` | 透传 `drop_headers=True`（丢弃 header/footer/page_number 版面块） | stderr 告警「已忽略」，继续识别 |
| `--crop-mode` | 显式给出时**原样传字符串**（`"auto"`/`"gundam"`/`"base"`），由引擎解析；不给则**不传该 kwarg**（引擎默认 `None`=旧行为） | 同左（`crop_mode` 参数名一直在，只是取值语义在引擎侧） |
| `truncated` / `pagesMismatch` / `groups` | 读并写进 JSON（`groups` 归一为 int 计数） | 字段缺失 → `false` / `0`，不报错 |

**`crop-mode` 传参口径（务必别改错）**：

- 不给 `--crop-mode` → CLI **不传** `crop_mode`（引擎 `None`）：单图 gundam、多页 base，
  与改造前完全一致；
- 给 `--crop-mode auto` → CLI 传字符串 `"auto"`，由引擎按长宽比判断：多页 → base，
  单图长截图（h/w > 3）→ base，其余单图 → gundam。**不能把 `auto` 塌缩成 `None`**，
  否则长截图自动降级不生效（长截图切片会退化成窄柱，慢且易截断）；
- 给 `gundam` / `base` → 传字符串，强制该策略（多页时 gundam 会被引擎降级为 base，
  因为上游只验证过单图切片）。

> 历史坑：`crop_mode` 在模型源码里最终是 `bool`（`bool(crop_mode)`），旧调用层
> 曾直接传 `True/False`。引擎已收敛为「`None` / 字符串」口径；CLI 只传字符串或
> 不传，**不再产生 bool**。

### 5.1 GUI 设置项与 CLI 的差异（含本轮新增的页范围 / 后处理开关）

GUI（设置页）把识别参数**持久化**在 `settings.json`，每次识别自动带上；CLI 的
对应能力**只作用于本次运行**，且部分开关 CLI 尚未提供（下表「CLI 对应」为 `—`
的项就是本轮**没有** CLI 开关的）。

| `settings.json` 键 | GUI 行为 | CLI 对应 | 说明 |
|---|---|---|---|
| `pdfPageRange`（默认 `""`） | 设置页填「PDF 页范围」，识别时按设置值透传给引擎（**只对 PDF 生效**，非 PDF 忽略） | `--pages "1-3,5,10-"` | 语法完全一致（同一份 `page_range.parse_pages`）。差别：GUI 是**持久设置**、CLI 是**单次参数**；GUI 的 A9 护栏（150 页上限）会**按选中页数**计算，CLI 侧没有该护栏（`--pages` 在本地筛页后再提交） |
| `joinLines`（默认 `false`） | 后处理把被硬换行拆开的同段文本接回去 | — | **本轮 CLI 无开关**：CLI 输出不做该后处理（需要的话在 GUI 里设，或后续给 CLI 加 `--join-lines`） |
| `cjkSpacing`（默认 `false`） | 后处理在中英文/数字之间补空格 | — | **本轮 CLI 无开关**，同上 |
| `dropHeaders`（默认 `false`） | 丢弃页眉/页脚/页码块 | `--drop-headers` | 两边都有；旧引擎不支持时 CLI 告警忽略 |
| `maxPageTokens`（默认 `4096`） | 单页输出 token 上限（0=不限） | `--max-page-tokens N` | 取值语义一致 |
| `maxMergedPages`（默认 `20`） | 合并请求的页数上限（0=不限） | `--max-merged-pages N` | 旧引擎上 CLI 自己分块（见上表） |
| `cropMode`（默认 `auto`） | 切片策略 | `--crop-mode auto\|gundam\|base` | 两边都透传字符串；CLI 不给则该 kwarg 不传 |

`page_range.parse_pages` 的语义（GUI 与 CLI 完全一致）：
1-based、逗号分隔的单页/闭区间/开区间，**越界夹取**并**升序去重**
（5 页 PDF 上 `1-3,5` → `[1,2,3]`；`10-` → `[5]`）；空串 = 不筛选。
GUI 里非法值会被设置页拒绝且**不写盘**（`err.badValue`）；CLI 里非法值直接是
参数错误（退出码 1，`CliError`）。

> `tableWarnings`（后处理发现的表格问题数）目前只透传到 **GUI 的识别响应与批量
> item**；CLI 的 `md/txt/json` 输出本轮**不含**该字段（CLI 未透传，后续需要再加）。

---

## 6. 与打包版（EXE）的关系 — 结论：打包版**不能**直接用 CLI

发行包由 `OCRMiniTool.spec` 构建，其中 `console=False`（GUI 子系统）。

因此：

- **双击 / 从资源管理器运行的打包版**没有控制台窗口；它的 stdout 没有去处，
  `--input`、`status`、`--json` 这些输出**你看不到**，管道/重定向也拿不到
  （Windows 上 GUI 子系统进程不继承 cmd 的 stdout）。**不要承诺"exe 加参数就能
  当 CLI 用"。**
- **`python cli.py …`（源码运行）完全可用**，这也是本文所有示例的前提；
  推荐用 `run.bat` 建好的 venv 里的 python。
- 要正式发布 CLI，需要**另出一个 `console=True` 的独立 EXE 目标**（例如
  `OCRMiniTool-cli.exe`），PyInstaller spec 里再加一个 `EXE(...)` 即可（可与
  GUI 版共享 `COLLECT` 的 `_internal`，体积增加很小）。这是打包侧改动，
  不在本文件范围内。

判定依据：`OCRMiniTool.spec` 的 `console=False`；姊妹项目 STTMiniTool 同样
`console=False` 却在 README 里宣传 `STTMiniTool.exe transcribe --json`，
这条"说明与实现不一致"的坑不要在本项目复刻。

---

## 7. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 退出码 2 + 「模型文件不完整或未下载」 | 先在图形界面「模型」页下载 Unlimited-OCR-2（约 6.8 GB），或把设置里的模型目录指对 |
| 退出码 2 + 「未检测到推理引擎组件」 | 在「模型」页按显卡下载 CPU/CUDA 推理组件，或在当前 env `pip install torch` |
| 退出码 2 + 「处理 PDF 需要 pymupdf」 | `pip install pymupdf`（GUI 里 PDF 识别同样需要它） |
| 退出码 2 + 「模型加载失败」 | 加 `--verbose` 看 traceback；常见是 CUDA 组件与显卡驱动不匹配，或显存不足 |
| 中文输出乱码（Windows 控制台） | 设 `set PYTHONUTF8=1`（应用本身启动时会设，直接调 python 时需要） |
| 想改模型目录 / 设备 | CLI 读 GUI 的 `settings.json`（`modelDir` 与 `devicePref`）；也可以用 `--device` 临时覆盖设备 |
| 输出文件重名 | 不会覆盖，自动写成 `a (1).md`、`a (2).md` |
| 目录里有非图片文件 | 静默跳过（按扩展名白名单过滤），空目录会以退出码 1 报「没有可识别的文件」 |

---

## 8. 已知限制

- **串行**：一次只跑一个文件、一个引擎实例；没有并发、没有任务队列、不支持断点续跑。
- **不支持 stdin** 输入（`--input -` 未实现），也不支持直接传图片字节。
- `--pages` 只对 PDF 生效；对图片输入会告警并忽略。
- 多文件识别不会合并成一份文档；每份结果独立落盘（合并可用 `jq`/脚本拼）。
- CLI 不写 `outputs/`、不改历史记录；它只读 `settings.json`，不写。
- `status` 刻意不加载 torch，因此不报 CUDA 是否可用；实际识别时会按 `--device` 自动检测。

---

## 9. 测试

```bash
python tests/run_tests.py test_cli     # 只跑 CLI 测试（不加载模型、不联网）
python tests/run_tests.py             # 全量
python tools/check.py                 # 静态体检（语法 / i18n / 行尾 / 版本口径）
```

`tests/test_cli.py` 用假引擎覆盖主流程，所有落盘都在临时目录；`cli.py` 的
`parse_pages` / `collect_inputs` / `build_arg_parser` 是纯函数，可脱离模型单测。
