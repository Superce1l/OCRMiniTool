# 构建笔记：打包版 EXE（onedir）+ 冒烟方案

> 状态：**第一阶段完成**（侦察 spec / 构建方案 / `tools/build_exe.py`）；
> **第二阶段进行中**——E（构建）与 F 前半（窗口/API/未装组件/干净退出）已实测通过
> （v1.3.0 与 A12 落地后的 v1.3.1 各构建/复验一次），F 后半（装 CPU 组件 / 加载 / 真实 OCR）
> 与 G（cu128 GPU 路径）等「可以装组件」后执行。
>
> 基线：`OCRMiniTool` 当前 `version.py` = **1.3.1**（A12 fp32 提交 `545e62b` 后），
> Win10 1903 x64，WebView2 157.0.4341.46，GPU GTX 1660 SUPER 6 GB。
> 构建脚本：`tools/build_exe.py`；测试：`tests/test_build_script.py`。

## 0. 一句话结论

**方案已验证可行**（实测：HEAD `187db41` 构建 **100 s** → `dist\OCRMiniTool\` 294.2 MiB /
3389 个文件，发行 zip 144.1 MiB、sha256 `be628aaf…`；打包版无模型时 **2.67 s** 出窗口；
CPU 组件 + 6.3 GB 权重下自动加载 **20 s**、单页 OCR **29.7 s**（A12 fp32 生效）；
cu128 + GTX 1660 SUPER 6 GB 下 CUDA 正常、**未 OOM**）。硬前提有两条：

1. 用**不含 torch** 的干净解释器构建（本机 `.venv` 解释器已坏且装着 torch 2.8.0，
   借用它的 `site-packages` 会让 `OCRMiniTool.spec` 预检立刻拒绝）；
2. 该解释器还必须装好**运行时依赖（除 torch/torchvision 外全部）**——只装
   `requirements-build.txt` 会「构建成功但产物是废的」（实测踩坑，见 §3.2.1）。

第二阶段（F/G）已全部实测完成，另外发现三个非预期行为（详见 §5.4 / §5.6）：
**① 自动加载把窗口出现时间从 2.7 s 拖到 20 s**；
**② CPU→cu128 组件切换（应用运行中）会 PermissionError、留 3.5 GB 残档**；
**③ 6 GB 显存「能跑但性能悬崖」**——长图 gundam decode 掉到 1.6 tok/s。

> §3.4 / §5.2 / §5.4 / §5.6 / §5.7 均已填入**实测**数据；§7 为发布前待补清单。

## 1. 现场事实（实测）

| 项 | 实测值 | 对构建/冒烟的含义 |
|---|---|---|
| `.venv` 解释器 | `.venv/pyvenv.cfg` 的 `home=C:\Users\X1C\...`（不存在）；`.venv\Scripts\python.exe -c "import sys"` → `uv trampoline failed to spawn Python child process / entity not found`，退出码 1 | **不能**用它构建；但它只是解释器坏了 |
| `.venv/Lib/site-packages` | 完好，52 个 dist-info（含 `pyinstaller 6.22.3`、`torch 2.8.0+cpu`、`transformers 4.52.1`…） | 可 `PYTHONPATH` 借来**跑源码**（实机报告 §5 的做法），但**不能**用于构建（见 §3.1） |
| 可用解释器 | `C:\Users\admin\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe` = Python 3.12.14，带 pip 26.2.1 | 建干净构建 venv 的来源 |
| `uv` | `C:\Users\admin\.local\bin\uv.exe` 0.13.7 | 可选的更快的建 venv 方式 |
| PyInstaller | 只存在于坏 `.venv` 的 site-packages（6.22.3） | 必须另装到干净 venv（`requirements-build.txt` 已锁 6.22.3） |
| WebView2 Runtime | 已装 157.0.4341.42 / 157.0.4341.46 | 冒烟前提满足 |
| `node` | v24.21.0（`tools/check.py` 的 JS 阶段用） | 与本任务无关，但门禁会用到 |
| 工作区完整性标签 | 在 `D:\Project` 下**新建**的文件带 `Mandatory Label\Low Mandatory Level:(I)(NW)`（`icacls` 实测） | 构建产物 `dist\` 也会带 Low IL → **.NET 拒载 pythonnet/WebView2 程序集**，冒烟前必须把产物拷出工作区或抬升标签（§5.1） |
| 网络 | GitHub 约 5.8 MB/s（lead 实测） | CPU 组件 272.9 MB ≈ 47 s；cu128 两卷 3.5 GB ≈ 10 min |

## 2. A. `OCRMiniTool.spec` 侦察

| # | 位置 | 事实 | 结论/影响 |
|---|---|---|---|
| A1 | `spec:31-36` | `importlib.util.find_spec("torch")` 为真且 `OCRMIT_ALLOW_BUNDLED_TORCH != "1"` → `SystemExit` | **预检在 Analysis 之前**，任何「能看到 torch 的解释器」都出不了包 |
| A2 | `spec:27-28,32,36` | `OCRMIT_ALLOW_BUNDLED_TORCH=1` 只跳过抛错；`Analysis(excludes=...)` 仍排除 torch | 语义是「兼容旧构建脚本的逃生门」，不是「允许打 torch 进包」 |
| A3 | `spec:95` | `excludes=['torch','torchvision','torchgen','functorch']` | 安装包不含推理引擎；组件由用户按显卡下载（`downloader._TORCH_FLAVORS`） |
| A4 | `spec:101-130` | onedir：`EXE(exclude_binaries=True)` + `COLLECT` | 产物 `dist\OCRMiniTool\OCRMiniTool.exe` + `_internal\`（PyInstaller 6 布局），零解压启动 |
| A5 | `spec:111` | `console=False` | 打包版**没有控制台** → `cli.py` 的 CLI 不能用（`docs/cli.md` §6 已写明；不属本批改动） |
| A6 | `spec:38-41` | `datas`：`assets/icon.ico → .`、`webview → webview_assets`（**整目录**） | 语言包覆盖 ✓：`webview/locales/*.json`、`webview/js/*`、`css`、`favicon` 全在 `webview_assets\` 下；`i18n.py:25-31` 从 `_MEIPASS/webview_assets/locales` 读，`webview_server.resolve_web_dir()` 同口径 |
| A7 | `spec:43-81` | `hiddenimports`：webview 平台后端、pythonnet/clr、certifi、addict/easydict/einops/tqdm；`collect_all(webview/pythonnet/clr_loader/tokenizers/pymupdf)`；`collect_submodules('transformers.models')`；torch 组件所需的 stdlib 冷门模块清单 | 静态 import 分析不到的部分（trust_remote_code、webview 平台、transformers 懒加载子模块）已显式补齐 |
| A8 | `spec:91` | `runtime_hooks=['runtime_hook_utf8.py']` | 冻结后仍设 `SSL_CERT_FILE`；注意 UTF-8 模式在运行时钩子里设**已经太晚**（本次构建脚本在启动器侧解决，不在本批） |
| A9 | 无 `version=` | spec 没有 Windows 版本资源 | EXE 属性里没有 FileVersion/ProductVersion → 记为「发布前必须补」（§7） |
| A10 | Analysis 入口只有 `app_webview.py` | `cli.py` 不被任何入口 import（实测 grep：只有 `tests/test_cli.py` 引用） | 打包版**不含 CLI 入口**；要发 CLI 需另一个 `console=True` 的 EXE 目标（`docs/cli.md` §6 结论一致） |

**需要在冒烟里实测的不确定点**

1. ~~**PDF 路径**：`ocr_engine.pdf_to_images()` 在函数内 `import fitz`……必须用真实 PDF 验一次~~
   → ✅ 已完成：打包版对 3 页 PDF 正常识别（§5.4），`fitz` 已随包可用。
2. **transformers 全量子模块**的收集是否会让 Analysis 阶段明显变慢（预计是构建耗时大头）。
3. 组件（torch）在冻结环境里 import 时依赖的 stdlib 清单是否还缺件——spec 已按经验列全，
   冒烟 CPU 组件安装后 `/api/load` 会直接暴露。

## 3. B. 构建方案（可复现）

### 3.1 为什么不能沿用「PYTHONPATH 借用 .venv」

源码运行可以（实机报告 §5：`.venv` 解释器坏了但 site-packages 完好，用任意 Python 3.12 +
`PYTHONPATH` 复用依赖即可）。**构建不行**，两个独立原因：

1. **预检必拒**：`PYTHONPATH=.venv\Lib\site-packages` 会让 `find_spec("torch")` 命中 →
   `spec:32` 直接 `SystemExit`（A1）。实测证据见 §3.3。
2. **构建不可复现/不可控**：PyInstaller 会在「dsh Python 3.12.14 + venv 的包」这种混合
   `sys.path` 下做分析；PyInstaller 自身的版本与依赖不在这个混合环境里受控，
   hook/依赖扫描结果随借来的包集合漂移；即使加 `OCRMIT_ALLOW_BUNDLED_TORCH=1` 绕过预检，
   也是「用侥幸绕过的环境产出官方发行包」，不值得。

### 3.2 干净构建 venv（推荐命令，可复制）

构建 venv **建在 `%LOCALAPPDATA%`**（避开 `D:\Project` 的 Low IL）：

```bat
rem ① 一次性：建一个不含 torch 的构建环境
set "PY312=C:\Users\admin\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
set "BUILDVENV=%LOCALAPPDATA%\OCRMiniTool\build_venv"
"%PY312%" -m venv "%BUILDVENV%"
"%BUILDVENV%\Scripts\python.exe" -m pip install --upgrade pip
"%BUILDVENV%\Scripts\python.exe" -m pip install -r requirements-build.txt

rem ② 验证：必须打印 None（同时确保没有设置 PYTHONPATH）
set "PYTHONPATH="
"%BUILDVENV%\Scripts\python.exe" -c "import importlib.util as u; print(u.find_spec('torch'))"
"%BUILDVENV%\Scripts\python.exe" -c "import PyInstaller, importlib.metadata as m; print(m.version('pyinstaller'))"
```

`uv` 等价（本机 uv 缓存里已有 CPython 3.12，通常更快）：

```bat
uv venv --python 3.12 "%LOCALAPPDATA%\OCRMiniTool\build_venv"
uv pip install --python "%LOCALAPPDATA%\OCRMiniTool\build_venv\Scripts\python.exe" -r requirements-build.txt
```

**本机已验证（第一阶段，未装任何包、未构建）**：`python -m venv` 6.2 s 建好（Python 3.12.14，
自带 pip 25.0.1），`find_spec('torch')` = `None`；`uv venv --python 3.12` 0.1 s（取 uv 管理的
CPython 3.12.16，注意 uv 建的 venv 默认不带 pip，包用 `uv pip install` 装）。两个探测 venv 已清理。

> pip 源默认为 PyPI；本机直连可达。若在代理环境，给 pip 加 `--index-url`/`--proxy`。

#### 3.2.1 ⚠️ 还必须装「运行时依赖（除 torch）」——本次实测踩到的坑

只装 `requirements-build.txt` 也能"构建成功"，但产物是废的：PyInstaller 只收集
解释器里**真实存在**的包。第一版构建（16 s、dist 32.7 MiB / 968 个文件、zip 13.6 MiB）
的 `build\OCRMiniTool\warn-OCRMiniTool.txt` 明确缺：`certifi`（`app_webview.py` 顶层
import）、`PIL`、`transformers`、`fitz`、`send2trash`；这样的 EXE 启动即
ModuleNotFoundError。

正确做法：把 `requirements-lock.txt` **去掉 torch/torchvision 两行**后装进构建 venv：

```bat
rem PowerShell：滤掉 torch / torchvision，其余运行时依赖都要在构建解释器里
powershell -NoProfile -Command "Get-Content requirements-lock.txt | Where-Object { $_ -notmatch '^(torch|torchvision)==' } | Set-Content '%LOCALAPPDATA%\OCRMiniTool\build_runtime_reqs.txt'"
"%LOCALAPPDATA%\OCRMiniTool\build_venv\Scripts\python.exe" -m pip install -r "%LOCALAPPDATA%\OCRMiniTool\build_runtime_reqs.txt"
rem 必须确认 torch 仍然缺席（否则 spec 预检会拒）
"%LOCALAPPDATA%\OCRMiniTool\build_venv\Scripts\python.exe" -c "import importlib.util as u; print('torch =', u.find_spec('torch'))"
```

`tools/build_exe.py` 的预检现在会检查 20 个运行时模块（`REQUIRED_RUNTIME_MODULES`），
缺任何一项直接拒绝构建并打印这条安装指引——这个坑不会再静默通过
（新增 3 条单测覆盖）。

### 3.3 预检拒绝的实测（本机已完成，未构建）

两条命令都只做 `find_spec`/元数据查询，不 import torch、不跑 PyInstaller（CPU 占用可忽略）。
`--allow-dirty` 只是为了绕过当前未提交的探查产物，与 torch 预检无关。

```bat
rem ① 基础解释器（没装 PyInstaller、看不到 torch）
"C:\Users\admin\.dsh\...\python.exe" tools\build_exe.py --dry-run --allow-dirty
```

```text
[FAIL] 当前解释器没有 PyInstaller：先 `pip install -r requirements-build.txt`，
       或按 docs/build-notes.md §3.2 建一个干净构建 venv。
[FAIL] 预检未通过，未执行构建。          → 退出码 2（实测）
```

```bat
rem ② 用 PYTHONPATH 借用坏 .venv 的 site-packages（能借到 PyInstaller 6.22.3，
rem    但 torch 也一起可见）——这正是要避免的姿势
set "PYTHONPATH=D:\Project\OCRMiniTool\.venv\Lib\site-packages"
"C:\Users\admin\.dsh\...\python.exe" tools\build_exe.py --dry-run --allow-dirty
```

```text
[FAIL] 当前解释器能看到 torch（D:\Project\OCRMiniTool\.venv\Lib\site-packages\torch\__init__.py）
       ——OCRMiniTool.spec 顶部预检会拒绝打包。请用**不含 torch 的构建 venv**（§3.2），
       不要用 PYTHONPATH 借用 .venv；确需硬绕过加 --allow-bundled-torch。
[FAIL] 预检未通过，未执行构建。          → 退出码 2（实测）
```

第 ② 条同时证明了：借 `PYTHONPATH` 后 **PyInstaller 6.22.3 是可见的**
（`importlib.metadata.version('pyinstaller')` = 6.22.3），也就是说「借包」唯一挡住构建的
就是 torch——这也说明为什么必须换干净 venv，而不是加个开关糊过去。


### 3.4 构建（E，已实测）

```bat
set "PYTHONPATH="
"%LOCALAPPDATA%\OCRMiniTool\build_venv\Scripts\python.exe" tools\build_exe.py --allow-dirty --zip
```

> `--allow-dirty` 只因为团队会话期间工作区里本来就有未提交的探查/在途产物
> （`git status` 8 项：`README.md`、`ocr_engine.py` 与 6 个新文件）；正式发布必须干净工作区。

| 次 | 构建解释器 / 源码版本 | 耗时 | dist | 文件数 | exe | zip | 结论 |
|---|---|---|---|---|---|---|---|
| 第 1 次（踩坑） | 只有 `requirements-build.txt` | **16 s** | 32.7 MiB | 968 | — | 13.6 MiB | 缺运行时依赖 → **废包**（§3.2.1） |
| 第 2 次 | + runtime 依赖（无 torch），v1.3.0 | 117 s | 294.2 MiB | 3389 | 29.7 MiB | 144.1 MiB | 可用；随后 HEAD 升到 v1.3.1 |
| 第 3 次 | 同上，A12 `545e62b` 之后 v1.3.1 | **97 s** | 294.2 MiB | 3389 | 29.7 MiB | 144.1 MiB | 中间产物 |
| **第 4 次（最终产物）** | 撤销 A6 长图降 base（`187db41`）后重建 | **100 s** | **294.2 MiB** | **3389** | 29.7 MiB | **144.1 MiB** | 当前 dist/zip |

- 版本/内容漂移处理：12:13 v1.3.0 → 12:23 A12（`545e62b`）→ 重建 v1.3.1 →
  13:38 撤销 A6（`187db41`）→ 再次重建；旧 zip 均删除，只保留当前产物。
- 当前产物：`dist\OCRMiniTool\OCRMiniTool.exe` sha256 `2dba0e2da5f3bd1c7b01d5c74108833d0f768af4eef98268dcf64a858838a692`；
  `dist\OCRMiniTool-v1.3.1-windows-x64.zip` = 151,144,024 B（144.1 MiB），
  sha256 `be628aaf7f77c6fafdd5ccfd300d0262a2b6781ff4ba771829f223313e162990`。
- 分发内容核对：`_internal\` 含 `webview/ tokenizers/ transformers/ pymupdf/ clr_loader/
  certifi/`，**无 `torch` 目录**（`find ... -iname '*torch*'` 只命中 transformers 里名字带
  pytorch 的 .py，符合预期）。
- 与 v1.2.0 对照：zip 144.1 MiB vs v1.2.0 的 144 MB —— 改造后体积没有回退。
- 耗时主要花在 Analysis（`collect_submodules('transformers.models')`），构建脚本打印
  `构建完成：97s`；总计含 preflight/zip 约 2.5 min。

## 4. C. `tools/build_exe.py` 行为

| 步骤 | 做什么 | 失败 |
|---|---|---|
| 1 定位 | 仓库根 = `tools/` 的父目录；检查 `version.py`、`OCRMiniTool.spec` 存在 | 退出 2 |
| 2 版本 | 从 `version.py` 的 `APP_VERSION` 读取（单一来源，不手写） | 退出 2 |
| 3 工作区 | `git status --porcelain -uall` 必须为空（`--allow-dirty` 可跳过；无 git 时告警继续） | 退出 2 |
| 4 torch 预检 | `find_spec("torch")` 必须为 None（`--allow-bundled-torch` 可绕过，但不推荐） | 退出 2 |
| 5 工具链 | `importlib.metadata.version("pyinstaller")` 必须可读；Python ≠3.12 只告警 | 退出 2 |
| 6 版本资源 | **D15**：写 `build\OCRMiniTool_version_info.txt`（`render_version_info()`，含 filevers/字符串表/Translation 1033·1200），在调 PyInstaller **之前**；`--dry-run` 不落盘 | 退出 2 |
| 7 构建 | `python -m PyInstaller --clean --noconfirm --distpath dist --workpath build OCRMiniTool.spec`（cwd=仓库根，透传 `OCRMIT_ALLOW_BUNDLED_TORCH`） | 退出 3 |
| 8 自检 | `dist\OCRMiniTool\OCRMiniTool.exe` 存在；打印体积/文件数/耗时/EXE 属性版本；`--zip` 打 `dist\OCRMiniTool-v<ver>-windows-x64.zip` 并打印 SHA256 | 退出 4 |
| 9 提示 | 未加 `--zip` 时打印 PowerShell 压缩命令 + exe 的 SHA256；产物在受管工作区时打印 Low IL 迁移提示（§5.1） | — |

退出码：`0` 成功 / `2` 预检失败 / `3` 构建失败 / `4` 产物缺失。脚本只写 `dist/`、`build/`
（均已 gitignore），不改任何源文件。

## 5. 第二阶段冒烟方案（F/G 的执行清单）

### 5.1 准备（两条最容易翻车的）

1. **单实例**：应用有单实例互斥（`app_webview.acquire_single_instance`），已有实例在跑时
   第二次启动**直接退出**。冒烟前：
   `powershell -Command "Get-Process OCRMiniTool -ErrorAction SilentlyContinue | Stop-Process -Force"`
2. **Low IL 迁移**：`D:\Project` 下新文件带 Low 完整性标签（§1 实测），而 pywebview/pythonnet
   加载 .NET 程序集时会被拒（`run.bat` 顶部注释记录过同一现象）。任选一种：
   ```bat
   rem A) 把产物拷出受管工作区（推荐，最贴近终端用户）
   robocopy dist\OCRMiniTool "%LOCALAPPDATA%\OCRMiniTool-smoke" /E /NFL /NDL /NJH /NJS
   rem B) 就地抬升完整性标签（管理员）
   icacls "dist\OCRMiniTool" /setintegritylevel M /t
   ```
3. 确认 `dist\OCRMiniTool\runtime\` 不存在（首次应是「未安装推理引擎组件」）。

### 5.2 启动与接口探测

| 检查 | 方法 | 期望 |
|---|---|---|
| 进程在跑 | `Get-Process OCRMiniTool` | 存在，`MainWindowTitle` 非空（原生窗口出现） |
| 端口 | `Get-NetTCPConnection -OwningProcess <pid> -State Listen` | 有一个 127.0.0.1 监听端口（`app_webview.py:453` 用 `port=0` 随机端口，**控制台不可用，只能这样发现**） |
| Host 校验 | 请求必须用 `http://127.0.0.1:<port>/...` 或 `http://localhost:<port>/...` | 其他 Host 会 403（`webview_server.py:_check_host`） |
| 版本 | `GET /api/status` | `"version": "1.3.0"`（取自 `version.py`） |
| 组件未装 | `GET /api/env` | `"installed": null`，`options` 含 cpu/cu128，`recommended` 按硬件推荐 |
| 文字提示 | 页面「推理引擎」卡片 / `/api/load` | 未装组件时应是**可读提示**，不是 500、不是静默无响应 |

**实测（F 前半，v1.3.0 12:12 与 A12 后的 v1.3.1 12:41 各跑一次，两次全部通过）**

- 迁移：`shutil.copytree` 拷 3389 个文件到 `%LOCALAPPDATA%\OCRMiniTool-smoke`（Low IL 规避成功）。
- 启动：进程起来 **3.16 s（v1.3.0）/ 2.67 s（v1.3.1）** 后出现可见窗口，标题 **`OCR 小工具`**
  （原生 WebView2，非 Edge 回退）；子进程里有 1 个 `msedgewebview2.exe`（父 pid = 应用 pid），
  窗口下有 `Chrome_RenderWidgetHostHWND` → WebView2 真的初始化了。
- 端口：`netstat -ano` 按 pid 发现 **56630（v1.3.0）/ 57040（v1.3.1）**；`GET /health` → 200
  `{"status":"ok","modelReady":false}`；Host 校验按 127.0.0.1 正常通过。
- `GET /api/status` → `version="1.3.0"` / **`version="1.3.1"`（重建后复验）**、
  `appName="OCR 小工具"`、`modelState="absent"`、`envInstalled=null`、`devicePref="auto"`。
- `GET /api/env` → `ok=true`、`installed=null`、`recommended="cu128"`（检测到 NVIDIA）、
  `options=["cpu","cu128"]`、`freeGb=201`。
- 未装组件/未下模型时 `POST /api/load` → 200 + **可读错误**
  `{"ok": false, "error": "模型尚未下载，请先下载模型。"}`，**进程存活**；随后
  `/api/status` 仍 `modelState=absent`、`envDownloading=false`（没有偷偷开始下载）。
- `GET /api/settings` → 200，键含 `cropMode/defaultModelDir/devicePref/dropHeaders/frozen/interpreter`。
- 关窗（`WM_CLOSE`）→ **exit code 0**，`tasklist` 无残留 `OCRMiniTool.exe`；
  `runtime/` 不存在，`outputs/` 已建，未产生 `settings.json`/`logs/`（本次没有任何用户动作/事件）。
- 截图证据：`D:\Project\_work\smoke1b\printwindow_parent.png`（识别页完整渲染：侧栏
  识别/模型/设置）。**注意**：窗口出现后 3 s 内用 `PrintWindow` 抓图会得到空白图，
  这是 WebView2 渲染时序问题，**不是白屏**；判定「白屏」要看三件套：
  `Chrome_RenderWidgetHostHWND` 存在 + `msedgewebview2.exe` 子进程 + 延迟后重抓。

### 5.3 CPU 组件链路（本次改造核心，必须真跑）

1. `POST /api/env/download`，body `{"flavor":"cpu"}`；进度从 `GET /api/events`（SSE）观察；
   下载 272.9 MB（5.8 MB/s ≈ 47 s，实测为准）。
2. 安装完成判据（`runtime\` 下）：
   - `torch_build.json`：`{"torch":"2.8.0","torchvision":"0.23.0","flavor":"cpu",…}`
   - `torch\__init__.py`、`torch\lib\*.dll` 存在；**下载的 zip 已被删除**（装完即删）
   - `logs\app-YYYYMM.log` 有「推理引擎组件安装完成：cpu（torch 2.8.0）」
3. **分卷/校验链路的现状**：单卷 cpu 走 `.part` 暂存 + HEAD 期望大小核对；
   cu128 两卷走 `_download_part`（每卷 `<part>.part` 暂存、大小核对后改名）+ 拼接。
   注意：**整包 sha256 仍未校验**（只有大小），发布侧 Release 里已有哈希 —— 记为发布前必补（§7）。
4. `POST /api/load` → `GET /api/status` 的 `envInstalled`/设备信息应反映 cpu 已装；
   日志出现模型加载与「识别计划/引擎指标」。

**实测（CPU 组件安装，12:36，部分放行；未加载模型）**

- `POST /api/env/download {"flavor":"cpu"}` → 200 `{"ok":true,"downloading":true}`；
  轮询 62.4 s 完成（下载 260.3 MB / 272,924,913 B，约 28.7 s ≈ 9.1 MB/s；其余为解压+标记）。
- 单卷 `.part` 链路实测可见：`...cpu.zip.part` 由 10.3 MB 递增到 272,924,913 B 后改名为
  `...cpu.zip`，装完**自动删除**（`leftover_zips=[]`、`leftover_parts=[]`）。
- `runtime\torch_build.json` = `{"torch":"2.8.0","torchvision":"0.23.0","flavor":"cpu",
  "installed_at":"2026-11-12 12:36:52"}`；`torch\__init__.py` 存在；`torch\lib` 11 个 DLL；
  **无 `torch_cuda.dll`（符合 CPU 版）**；`runtime\` 共 1,384,511,877 B / 15,519 个文件。
- `/api/env` → `installed.flavor="cpu"`；`options`：cpu `installed=true`、cu128 `installed=false`；
  `selected="cpu"`、`recommended="cu128"`（硬件推荐与已装不同，符合预期）。
- 日志三行：`cpu.zip.part: download OK (260.3 MB)` / `推理引擎组件安装完成：cpu（torch 2.8.0）` /
  `推理引擎组件就绪：cpu / torch 2.8.0（60s）`。
- 安装期间可用内存始终 1.4–1.6 GB（与并行校准任务共存，未越界），关窗 exit 0。
- ⚠️ 幂等/`quick_check_torch` 的「再点一次应 `already=true`」这次**没验到**：带 modelDir
  重启应用时它**自动开始加载模型**（`logs`: `12:37:55 [MODEL] 开始加载 …`），`/api/env/download`
  先被 busy 挡下。已在约 60 s 内 kill；该 auto-load 属既有设计（模型+组件就绪即自动加载），
  留到 F 后半一并验证（预计无需再手动 `POST /api/load`）。

### 5.4 真实 OCR（F 后半，已实测）

`POST /api/recognize` JSON `{"paths":[...]}`；打包版 + 本机 6.3 GB 权重 + CPU 组件：

| 用例 | 结果 | 耗时 | 引擎指标（logs） | 产物 |
|---|---|---|---|---|
| 启动自动加载（cpu） | `modelState=ready`、`loadedDevice=cpu`、torch `2.8.0+cpu` | 加载 **20 s**（log「模型加载完成（cpu，20s）」） | 视觉塔 `mode=auto → fp32` | — |
| `test_small.png`（单页，gundam） | 200，1 页，`finish=stop` | **29.7 s** | prefill 21.3 s / decode 8.3 s / 75 tok / **9.1 tok/s** | `outputs/test_small.md`（109 B，内容正确） |
| `test_pages.pdf`（3 页，base） | 200，3 页，`finish=stop` | **74.7 s** | prefill 32.7 s / decode 40.5 s / 296 tok / 7.3 tok/s | `outputs/test_pages.md`（389 B，含分页标记） |

- **A12 打包版验证成立**：单页 29.7 s（旧逻辑量级 120 s），视觉塔 fp32 生效（log 明写）；
  瓶颈与校准一致——decode 占大头（图片页 28%、PDF 页 54%）。
- **PDF/`fitz` 打包可用**（§2 A9 疑点解除）：3 页 PDF 正常转图+识别，无 ModuleNotFoundError。
- 内存：加载后可用内存 13.2 → 10.1 GB，OCR 期间最低 **1.2 GB**（16 GB 机器 + 6.3 GB 权重，
  接近上限；此时再并行第二个引擎必 OOM——与团队「不同时跑两个模型」的纪律一致）。
- **启动顺序问题（新发现）**：模型/组件就绪时 `start_load()` 在 `run_native_window()` **之前**
  （`app_webview.py:471-478` vs `:488`），torch 导入+权重加载抢 GIL/磁盘，
  **窗口出现时间从 2.67 s 拖到 20.1 s**（同一产物、只差「有没有模型可自动加载」）。
  用户感知是「双击后 20 秒无窗口」。建议先把窗口/首帧建出来，再触发自动加载（或延后 500 ms）。

> 配套校准结论（由校准批次实测，供交叉引用）：fp32 后单页 120.3 s → 18.8 s，**瓶颈转移到
> decode**（4–9 tok/s，长输出页 decode 占 90%）；`maxPageTokens` 校准（密集正文/表格/公式/
> 双栏/代码 tokens 1020–2333）**4096 默认值有 75% 余量、不改**；超长截图（h/w>5）在 gundam 下
> 2713 token / finish=stop / 正文连贯，在 base 下会退化重复（36 min / 12285 字未停）——
> 所以「长图别用 base」已写进 README 与界面文案（包内代码为 `187db41` 撤销自动降 base 后的版本）。

### 5.5 退出是否干净

关窗或 `POST /api/shutdown` → 进程退出；无残留子进程（Job Object 会连带清理）；
日志无 traceback；再启动不报单实例占用。

**已实测**：`WM_CLOSE` 关窗后 exit code **0**，30 s 内退出，`tasklist` 无残留；
再次启动可正常拿到窗口（单实例互斥已释放）。

### 5.6 GPU（cu128）实测结果

**安装链路（D6 续传修复真验）**：干净 runtime 启动 → `runtime\` 放完整 part02 +
`part01-of-2.part`（1,887,436,800 B，比期望少 100 MiB，且无 part01 最终名）→
`/api/env/download {"flavor":"cu128"}`：

- 日志 `...part01-of-2.part: download OK (1900.0 MB)` → Range **只补了缺的 100 MiB**；
- 随后 parts 清空、出现整包 `...cu128.zip` 3,499,385,137 B（拼接）→ 解压 →
  `torch_build.json` flavor=cu128（14:10:12），总耗时 **66.3 s**。
  **「暂存名 → HEAD 期望大小 → Range 续传 → 大小核对 → 改名 → 拼接 → 解压 → 标记」实测通过**；
  未覆盖：两卷都从零全量下载（放置法只能验后半段）。

**CUDA 检测 / 加载 / 推理（GTX 1660 SUPER 6 GB）**

| 项 | 结果 |
|---|---|
| CUDA 检测 | `torch=2.8.0+cu128`、`cuda=true`、`cudaBuild=12.8`、`cudaName=NVIDIA GeForce GTX 1660 SUPER`、`vramGb=6.4` |
| 冷加载 | **67.9 s** 到 `modelState=ready`、`loadedDevice=cuda`；视觉塔 `mode=auto → bf16`；VRAM 峰值 **5875/6144 MiB**（余量 ~270 MiB） |
| 二次加载 | **12 s**（OS 缓存热） |
| 单页 `test_small`（gundam） | 32 s；prefill 21.7 s / decode 10.1 s / 75 tok / 7.4 tok/s；**与 CPU（30 s）几乎相同**（decode 主导，GPU 无收益） |
| 长图 `test_long`（h/w>5，gundam 多 tile） | **1017 s（≈17 min）**；prefill 45.4 s / decode **971.7 s** / 1521 tok / **1.6 tok/s**；`finish=stop`、未截断；VRAM 5801–5807/6144；**无 OOM**、进程存活、输出正常 |

**结论**：
- 「6 GB 显存放不下 6.3 GB 权重」**不成立**——能加载、能跑（峰值 5.87 GB）。
- 但 6 GB 是**「能跑但性能悬崖」**：余量 <300 MiB，长图/多 tile 时 decode 从 7.4 tok/s 掉到
  **1.6 tok/s**（很可能比 CPU 还慢）。`auto` 切片对小图也是 gundam 多 tile，显存压力比 base 大。
- **本次没触发 OOM**，所以「OOM 错误是否可读 / 是否回退 CPU」**没有实测数据**；按代码推断
  OOM 会以 `CUDA out of memory` 抛到现有加载/识别失败路径（无自动回退），错误信息可读但不会自愈。
- 建议 README/界面补一句：6–8 GB 显存建议短页或直接 CPU；长图用 CPU 更稳。

**组件切换缺陷（重要，新发现）**：CPU 组件已装、应用运行中（已 import runtime 下的 torch/PIL）
→ 装 cu128 时解压覆盖 `runtime\PIL\_imaging.cp312-win_amd64.pyd` 报
`PermissionError: [Errno 13] Permission denied`——应用进程把该 pyd 当 DLL 加载，
Windows 不允许覆盖已加载 DLL。后果：安装失败（`envDownloading` 复位、marker 仍是 cpu、
**残留 3.5 GB 整包 zip**、无回滚），进程存活。删掉/换新 runtime 后安装成功（本次 66.3 s）。
修复方向：解压到临时目录后原子替换，或明确提示「切换组件需退出应用」并在下次启动完成安装。
证据：`D:\Project\_work\smoke3\fail_cpu_to_cu128_applog.txt`。

**下载链路与网络（实测）**：cu128 两卷 12:28 全部下完，sha256 与 GitHub asset digest 一致，
拼接整包 sha256 `6f10c1b7…` 与 Release 说明一致（20.6 s 流式计算）→ 预下载字节可信。
应用自身仍只对账大小、不校验整包 sha256（见 §7 第 2 条）。

### 5.7 第二阶段进度快照

| 步骤 | 状态 | 关键结果 |
|---|---|---|
| E 构建 | ✅ | v1.3.0 117 s → A12 后 97 s → **HEAD 187db41 重建 100 s**；dist 294.2 MiB / 3389 文件；最终 zip sha256 `be628aaf…` |
| F① 窗口出现 | ✅ | 无模型/组件时 2.67 s；**模型+组件就绪自动加载时 20.1 s**（见 §5.4 启动顺序问题） |
| F② 端口 + `/api/status` | ✅ | 端口随机可发现；`version=1.3.1`、`modelState=absent`/`downloaded` 均正确 |
| F③ 未装组件提示 | ✅（API 层） | `/api/env installed=null`；`/api/load` 可读错误、进程存活 |
| F④ CPU 组件 + 加载 + 真图 OCR + PDF | ✅ | 组件 62.4 s；加载 20 s；图片 29.7 s（fp32 生效）；PDF 74.7 s（`fitz` 打包可用） |
| F⑤ 关窗干净退出 | ✅ | exit code 0，无残留进程 |
| G cu128 GPU | ✅ | 续传安装 66.3 s；CUDA 12.8 检测成功；冷加载 67.9 s、VRAM 峰值 5.87 GB；**无 OOM**；长图退化到 1.6 tok/s |
| G 附加 | ⚠️ | 组件切换（CPU→cu128，运行中）PermissionError + 3.5 GB 残档；启动自动加载把窗口推迟到 20 s |


## 6. 已知风险（构建/冒烟维度）

| 风险 | 影响 | 缓解 |
|---|---|---|
| Low IL 产物 | pywebview 原生窗口起不来（可能静默回退 Edge 或白窗） | 冒烟前拷出 `D:\Project` 或 `icacls /setintegritylevel M`（§5.1） |
| 打包版缺 stdlib 冷门模块（torch 组件需要） | `/api/load` 报 ImportError | spec 已按经验列清单；实测缺件再补 spec（需单独批次） |
| `fitz` 收集不确定 | PDF 识别失败（图片正常） | ✅ 已闭环：3 页真实 PDF 在打包版正常识别（§5.4） |
| 组件整包无 sha256 | 镜像/劫持/重切片只能靠大小与解压报错 | 记入 §7 |
| 打包版无 CLI / 无 EXE 版本资源 | 发布形态不完整 | 记入 §7 |
| 6 GB 显存 | 不 OOM，但余量 <300 MiB；长图 gundam decode 掉到 1.6 tok/s（17 min/页） | README/界面加「6–8 GB 建议短页或 CPU」；不当作 bug 修 |
| **组件切换（CPU↔cu128）会失败** | 运行中安装另一 flavor → 覆盖已加载 DLL 报 PermissionError，滞留 3.5 GB 残档、无回滚 | 记入 §7；短期提示「切换组件请先退出应用」 |
| 构建 venv 缺运行时依赖 → 废包 | EXE 启动即 ModuleNotFoundError（实测已发生一次） | ✅ 已闭环：构建方案改为「lock 去掉 torch 后安装」，且 `build_exe.py` 预检硬拦 20 个运行时模块 |
| WebView2 首帧时序 → 误判白屏 | 启动后 3 s 内 `PrintWindow` 抓图是空白 | 判定看三件套（RenderWidgetHost + msedgewebview2 子进程 + 延迟重抓），本次已用截图证实 UI 完整渲染 |
| GitHub release asset 网络抖动 | 组件下载/预下载失败（F 后半、G 阻塞） | 短超时+续传+退避重试；持续不可达则需换网络/代理（状态见 `_work/cu128_dl_status.json`） |
| **自动加载抢在窗口创建之前** | 无模型 2.7 s 出窗口，有模型/组件时 **20.1 s**，用户感知「双击后 20 秒无响应」 | 记入 §7（窗口先出、再触发自动加载） |

## 7. 发布前必须补（第二阶段实测后更新）

1. ✅ **EXE 版本资源（D15 已落地）**：`tools/build_exe.py` 构建前由
   `render_version_info()` 写 `build/OCRMiniTool_version_info.txt`，`OCRMiniTool.spec`
   动态 `EXE(version=_verfile)` 读取（spec 不写死版本号）；EXE 属性里可见
   FileVersion/ProductVersion=APP_VERSION。另有 `tools/release_check.py`（D4）把关
   版本↔tag↔工作区↔远端↔zip，见 §9。
2. ✅ **组件包整包 sha256 校验（D5 已落地）**：解压前校验整包哈希，默认 warn-only，
   `OCRMT_VERIFY_RUNTIME=strict` 时删包抛错；发布侧需上传 `<zip>.sha256` / `manifest.json`
   （否则应用只能 WARN 跳过）。详见 §8。
3. **`tools/build_exe.py` 接入 CI**（windows runner）：构建 + 最小冒烟（进程/`/api/status`/
   未装组件提示），防止「源码能跑、打包版不能跑」回归。
4. **独立 `console=True` 的 CLI EXE**（若 CLI 要进发布形态，`docs/cli.md` §6）。
5. **发行 zip + SHA256SUMS + tag↔HEAD 一致性**：脚本预检已挡脏工作区；发布流程仍要贴哈希
   （v1.2.0 的 Release 说明没有主 zip 哈希）。
6. **低显存机器的可读指引**：cu128 能跑但 6 GB 长图会掉到 1.6 tok/s；README/界面建议
   「6–8 GB 显存：短页或 CPU；长图用 CPU」，并把「≥8 GB 显存」标成推荐值而非硬性要求。
7. **组件切换缺陷（新增，优先级高）**：CPU↔cu128 切换在应用运行中会 `PermissionError`
   （覆盖已加载 DLL）并留 3.5 GB 残档。修复方向：解压到临时目录后原子替换，
   或「切换需重启应用」的显式流程（下次启动完成安装）。
8. **启动顺序（新增）**：`start_load()` 早于 `run_native_window()`，模型/组件就绪时窗口
   推迟到 20 s。建议先建窗口/首帧再自动加载，或延后 500 ms 低优先级触发。

## 8. 组件发布与校验（D5）

推理引擎组件（CPU ≈273 MB / cu128 两卷 ≈3.5 GB）以前只有 Content-Length 一个完整性信号；
D5 补上**整包 sha256**：下载/分卷拼接完成、**解压之前**校验一次，与发布产物对齐。

### 8.1 发布 Release 要上传什么

tag 与 `downloader._TORCH_TAG` 一致（`runtime-torch<torch 版本>`），资产清单：

| 资产 | 说明 |
|---|---|
| `ocrmini-runtime-torch<v>-cpu.zip` | CPU 组件整包（默认不分卷） |
| `ocrmini-runtime-torch<v>-cu128.zip.part01-of-N` … `partNN-of-N` | cu128 超过 GitHub 单资产 2 GB，**默认按 1900 MiB 分卷**（`--split`）；命名 `{zip}.part{i:02d}-of-{N}`，**N 不补零**，与 downloader 拼接的 URL 完全一致 |
| `<zip>.sha256` | **整包**哈希，`sha256sum` 格式 `<hex>  <文件名>`（应用取第一个 64 位 hex） |
| `SHA256SUMS` | 所有 zip 行 + 各分卷行 + `manifest.json` 行的 sha256 清单（人肉核对/发布说明用） |
| `manifest.json` | `build_components.py` 产出；顶层按 zip 名索引：`{flavor,torch,torchvision,size,size_mb,sha256,tops,parts:[{name,size,sha256}]}`；downloader 读取兼容（只取 `sha256`，也兼容 `{"zips":{…}}`） |

`build_components.py` 会自动产出后三个文件（`write_sidecars`），打印上传指引：

```bat
uv run python build_components.py                 rem 两个都做；cu128 默认切成 1900 MiB 两卷
uv run python build_components.py cu128 --split 1900
uv run python build_components.py cpu --split 0   rem 不切
uv run python build_components.py --no-deps --pins "sympy==1.14.0,networkx==3.7,jinja2==3.1.6"
```

- **切完保留原 zip**：sidecar/整包 sha256 都以原 zip 为准；**发布时 split flavor 只传 parts**，
  原 zip 留本地。
- **版本单一来源**：`TORCH_VER/TV_VER` 必须等于 `downloader._TORCH_VERSION/_TORCHVISION_VERSION`，
  main 启动先校验（`version_mismatch()`），不一致直接拒绝构建。
- **`--no-deps`/`--pins` 风险**：`--pins` 就是完整的依赖集（torch/torchvision 之外还要
  `sympy/networkx/jinja2/filelock/fsspec/mpmath/setuptools…`），漏一个就是组件缺件
  （历史实录：缺 sympy → `import torch` 失败）。默认不带 `--no-deps`，由 uv 正常解析。

### 8.2 应用侧期望哈希的来源（优先级从高到低）

1. **`_TORCH_FLAVORS[flavor]["sha256"]`**（`downloader.py`）——发布后把整包哈希回填到这里，
   离线/断网也能校验；空串 = 不启用该来源。
2. **远端 `<zip_url>.sha256`**——与 zip 同目录的小文件；缺失/非 hex/网络失败只 WARN。
3. **远端 `manifest.json`**——兼容两种结构：扁平 `{"<zip名>": {"sha256": …}}`，
   或 `{"zips": {"<zip名>": {"sha256": …}}}`；同样容错。

三个来源都拿不到 → 记 WARN「拿不到期望 sha256」并**跳过校验**（不阻断安装）。

### 8.3 两种模式

| 模式 | 触发 | 哈希不匹配时的行为 |
|---|---|---|
| `warn`（默认） | 不设环境变量 | `log_download` 写 `WARN 组件 sha256 不匹配：<flavor> 期望 …（source=…），实得 …；warn-only，继续安装`，安装继续 |
| `strict` | `set OCRMT_VERIFY_RUNTIME=strict` | 删除 zip、分卷与 `.part`/`.concat.part` 残档，抛 `RuntimeError("组件包校验失败：<zip> sha256 不匹配…请检查网络/镜像后重试。")`；UI 经现有 `dl.failed` 文案显示，**不解压、不写 marker** |

边界：**拿不到期望值**时 strict 也只 WARN、不删包（不能因为发布侧漏传哈希就毁掉用户的下载）。
远端哈希端点本进程内失败过一次后会记住（`_REMOTE_HASH_DOWN`），不再反复等网络。

### 8.4 与 D6 分卷续传的关系

校验只读**拼接后的 final zip**（与发布整包一致），不动每卷 `<part>.part` 暂存/HEAD 核对/改名
逻辑；分卷先拼接、后校验、再解压。因此 D6 的续传修复与本校验互不影响
（`tests/test_build_downloader_parts.py` 的 12+ 用例保持全绿）。

**构建侧（D7）与下载侧的衔接**：

- `build_components.split_file()` 命名 `{zip}.part{i:02d}-of-{N}`，**N 不补零**——与
  `downloader.download_torch()` 的 `f"{meta['zip']}.part{i:02d}-of-{n_parts}"` 完全一致，
  否则应用拼出的下载 URL 会 404。
- `downloader._TORCH_FLAVORS[flavor]["parts"]` 必须等于构建产出的分卷数：当前 cu128
  整包约 3.34 GB、`--split 1900` → **2 卷**；如果换 torch 版本导致体积跨过 1900×N 边界，
  两边要同时改（`version_mismatch()` 只校验版本号，不校验卷数）。
- 构建侧 manifest 里的 `parts[{name,size,sha256}]` 目前供人工核对/发布说明使用；
  downloader 的分卷校验仍靠 HEAD 字节数（D6），整包 sha256 在拼接后校验（D5）。
  将来若要把逐卷 sha256 下发给应用，可直接读这个 `parts` 字段（结构已就绪）。

### 8.5 已发布资产的哈希（可回填）

发布侧已有哈希（与 Release 说明/API asset digest 一致，见
[04-build-dist.md](explore/04-build-dist.md) §4.4）：

```
69ec27add77adbf3bf2eb78b00066eb816d8f040cc61c57cbd55b264f2198f09  ocrmini-runtime-torch2.8.0-cpu.zip
6f10c1b7f907aed7cf217c6d5c3a4833267deda4d79718b8de35649d75990b9c  ocrmini-runtime-torch2.8.0-cu128.zip（两卷拼接后的整包）
```

回填方式：改 `downloader._TORCH_FLAVORS["cpu"]["sha256"]` / `["cu128"]["sha256"]`，
或用远端 `.sha256`/`manifest.json` 让应用自动取。发布新组件时用构建机重新算：

```bash
sha256sum dist_components/ocrmini-runtime-torch<v>-cpu.zip
cat dist_components/*.part01-of-2 dist_components/*.part02-of-2 > /tmp/cu128.zip
sha256sum /tmp/cu128.zip
```

`build_components.py` 生成的 `<zip>.sha256`、`SHA256SUMS` 与发布说明里的哈希是同一批值。

### 8.6 本地验证

```bash
PY="C:/Users/admin/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe"
"$PY" tests/run_tests.py test_component_sha256     # 16 例，全本地 http.server，无外网
"$PY" tests/run_tests.py                          # 全量回归
```

## 9. 发布流程（D15 + D4）

### 9.1 一条链路：改版本 → 构建（带 EXE 版本资源）→ release_check → 上传

1. **版本与 tag**：改 `version.py` 的 `APP_VERSION`（唯一来源）→ 提交 → `git tag v<ver>`
   **指向 HEAD** 并推送（`git push origin v<ver>`）。`release_check` 会校验 tag 与
   APP_VERSION 一致、tag 存在且指向当前 HEAD。
2. **构建**（干净构建 venv，见 §3.2）：
   ```bat
   "%LOCALAPPDATA%\OCRMiniTool\build_venv\Scripts\python.exe" tools\build_exe.py --zip
   ```
   - 调 PyInstaller **之前**先写 `build\OCRMiniTool_version_info.txt`
     （`render_version_info()`：filevers/prodvers、FileVersion/ProductVersion、
     ProductName=OCR Mini Tool、FileDescription、CompanyName、LegalCopyright、
     Translation 1033/1200）；`OCRMiniTool.spec` 用 `EXE(version=_verfile)` 动态读取，
     **spec 里没有写死版本号**。
   - 验证 EXE 属性：资源管理器 → 属性 → 详细信息；或
     `powershell "(Get-Item dist\OCRMiniTool\OCRMiniTool.exe).VersionInfo | fl FileVersion,ProductVersion"`
   - `--zip` 产出 `dist\OCRMiniTool-v<ver>-windows-x64.zip` 并打印 SHA256。
3. **发布前检查**：
   ```bat
   python tools\release_check.py                     rem 默认 tag=v<APP_VERSION>、zip=dist 默认名
   python tools\release_check.py --offline           rem 跳过远端 tag（离线/无 origin）
   python tools\release_check.py --write-sums        rem 额外写 SHA256SUMS.txt
   ```
   - 逐项 ✓/✗：a 版本↔tag、b 工作区干净、c tag 存在且指向 HEAD、d 远端 origin 有该 tag、
     e zip 名含版本 + sha256（末尾打印）。
   - 退出码 `0` 全过 / `1` 有失败项 / `2` 参数/环境错误（非 git 仓库、tag 格式非法、
     `--write-sums` 但 zip 不存在）。
   - `--write-sums` 在 zip 同目录写 `SHA256SUMS.txt`，内容 `<hex>  <basename>`（LF）。
4. **上传 GitHub Release**（tag = `v<ver>`）：主 zip + `SHA256SUMS.txt`；
   推理组件的资产清单见 §8.1（组件 zip/分卷 + `<zip>.sha256`/`SHA256SUMS`/`manifest.json`）。
5. **上传后自检**：`release_check --offline` 再跑一次，确认 zip sha256 与 Release 说明一致。

`tests/test_build_release.py`（20 例，mock git、不联网、不跑 PyInstaller）覆盖本流程的
版本资源渲染、spec 接线、检查项与退出码。

### 9.2 本机无法验证的点（无构建环境）

- **EXE 资源是否真的生效**：本批**不跑全量 PyInstaller 构建**，只验证了 VSVersionInfo
  文本与 spec 的 `version=_verfile` 接线；首次真机构建后用上面第 2 步的 PowerShell/
  属性面板抽查一次即可关闭该风险。
- **代码签名**：未配证书，`release_check` 也不检查签名（SmartScreen 提示仍按 §5.1 处理）。
- **远端 tag 项**：本地未 `git fetch --tags` 时 c/d 会失败——先 `git fetch --tags` 再跑，
  或用 `--offline` 只做本地检查。
