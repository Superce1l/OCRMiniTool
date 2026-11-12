# 04 · 构建 / 分发 / 工程化 探查报告

- 基线：`OCRMiniTool @ ecd717a`（`main` = `origin/main`），`version.py:3 APP_VERSION="1.2.0"`
- 对照：`../STTMiniTool @ 2.3.0`（同作者，只读；有 tests/、pyproject.toml、CI、requirements-lock.txt）
- 口径：**事实**＝有 `文件:行号` 或命令输出；**推断**＝按代码/文档/发布资产推理，未实跑；**建议**＝可执行动作
- 环境限制：本机 `.venv` 损坏、无可用 PyInstaller 构建环境（不能 `pip install`、不能真打包）→ 所有「打包后行为」均标注为推断
- 探查时段：2026-11-12 09:36–09:52（+08）。`tests/`、`tools/check.py`、`docs/explore/05-*.md` 是同期其他队员的**在途产物**（探查开始时不存在），不计入基线事实
- **时效说明**：写作过程中团队已并行落地两项整改——`afdc03e chore(repo): 补 .gitattributes 统一行尾`（含 `tools/normalize_eol.py`，已消除 20 文件假脏）、`b33cab2 test: 新增 101 用例回归套件 + 静态体检脚本`。下文事实链以探查起点 `ecd717a` 为基线；D 清单中已落地项标注「✅ 已落地」

---

## 0. 结论速览（Top 5）

| # | 结论 | 关键证据 | 级别 |
|---|---|---|---|
| T1 | 工作区 100% 脏：20/20 文件仅因 LF→CRLF 而「全量修改」，无 `.gitattributes` / `.editorconfig` 兜底 | `git status --porcelain`=20 行；`git diff --numstat` 每文件增=删（如 `webview_backend.py 994/994`）；`git ls-files --eol` i/lf w/crlf | 高 |
| T2 | 3.3 GB 推理组件包**全程无哈希校验**，且分卷续传可静默产出坏包（历史日志有实录） | `downloader.py:188-223` 只对 Content-Length；`grep sha256 downloader.py` 仅命中注释；`logs/app-202611.log` 14:14–14:20 两次 part01 大小 1437.3→1900.0 MB 均「download OK」 | 高 |
| T3 | 依赖口径三方漂移，README 的 GPU 安装命令会装出**自家文档判定必坏**的组合 | `requirements.txt:5`（torch 2.13）/`:20`（`torch>=2.5`）；`build_components.py:30-33`（必须 <2.9）；`downloader.py:244`（2.8.0）；`README.md:166-169`（cu128 不钉版本）；`ocr_engine.py:9`（2.13） | 高 |
| T4 | 发布链路手工、无脚本、不可复现：tag 指向非构建源、资产由脏工作区产出、`build_components.py` 不产分卷、manifest 不上传、主 zip 无 SHA256 | `git ls-remote --tags origin`（v1.2.0=runtime-torch2.8.0=4bf8165）；GitHub API 资产上传时间 06:55:34Z vs `ecd717a` 14:55:54+08:00；`build_components.py:67-78` 只写单 zip；`downloader.py:508` 要 part01-of-2 | 高 |
| T5 | 工程基线缺 CI / lint / tests / lock；`.venv` 因跨用户路径已坏且 `run.bat` 只按「文件存在」就选中它 | 仓库 35 个 tracked 文件无 `.github/ tests/ pyproject.toml lock`；`.venv/pyvenv.cfg:1 home=C:\Users\X1C\...`（不存在）；`.venv/Scripts/python.exe` → `uv trampoline failed to spawn Python child process`；`run.bat:24` | 中高 |

---

## 1. 仓库卫生

### 1.1 行尾（EOL）事故：事实链完整（✅ 已由 `afdc03e` 修复）

> 落地校验：`afdc03e` 的 `.gitattributes` 与 D1 建议一致（`* text=auto eol=lf`、`*.bat/*.cmd eol=crlf`、二进制清单），并附 `tools/normalize_eol.py`；当前 `git ls-files --eol` 除 `run.bat`（`w/crlf`，符合约定）外全为 `w/lf`，`git status` 不再有 19 个假脏文件。下面是**修复前**的证据链，保留作回归判据。

| 证据 | 输出 / 说明 |
|---|---|
| `git status --porcelain` | 20 行 ` M`，无 `??`、无删除 |
| `git diff --numstat` | 每文件 `+N/-N` 且 N 相同（`app_webview.py 427/427`、`downloader.py 676/676`…）＝纯行尾差异，无内容改动 |
| `git ls-files --eol` | 18 个文本文件 `i/lf w/crlf`；`.gitignore` 甚至 `w/mixed`；`README.md build_components.py i18n.py motw_unblock.py version.py webview/js/i18n.js locales/*.json` 仍 `w/lf` |
| `ls .gitattributes` | 不存在；`git config core.autocrlf` / `core.eol` 均未设置（无输出） |

**影响**：任何 `git commit -a` 会生成 6042 行「纯空白」提交，reviewer 看不到真实改动、`git blame`/`bisect` 被污染；混用编辑器时每次保存都再次翻脏；GitHub ZIP/他人 clone（LF）与本地（CRLF）不一致。
**建议**：新增 `.gitattributes`，再 `git add --renormalize .` 收口，见 D1。
**风险**：低（不改内容）；唯一硬约束是 `*.bat` 必须 CRLF。
**验证**：执行后 `git status` 为空、`git ls-files --eol | grep -v 'w/lf'` 仅剩故意 CRLF 的文件。

### 1.2 `.gitignore` 缺口（实测 `git check-ignore`）

| 探测路径 | 结果 | 说明 |
|---|---|---|
| `dist_components/x.zip` | 已忽略（`.gitignore:18 dist_*/`） | 组件 zip 产物 OK |
| `OCRMiniTool-v1.2.0-windows-x64.zip` | **未忽略** | 发行包落仓库根就脏；GitHub 100MB 限制会在推送时才炸 |
| `_internal/…`、`Thumbs.db` | **未忽略** | Windows 项目典型遗漏 |
| `.ruff_cache/x`、`.pytest_cache/`、`.mypy_cache/` | **未忽略** | 一旦引入 lint/测试就脏 |
| `.vscode/settings.json` | 被忽略 | 但原因是 `.gitignore:5 settings.json` 的**无斜杠全局模式**误伤（同名文件在任意层级都被忽略） |

**影响**：低—中（噪音 + 误提交大文件风险）；全局 `settings.json` 规则还会吞掉将来任何子目录同名文件。
**建议**：补 `*.zip`、`_internal/`、`Thumbs.db`、`desktop.ini`、`.ruff_cache/`、`.pytest_cache/`、`.mypy_cache/`、`.vscode/`；把 `settings.json` 收紧为 `/settings.json`（见 D14）。

### 1.3 `.venv` 已损坏（实测，可作为独立工程发现）

| 证据 | 输出 |
|---|---|
| `.venv/pyvenv.cfg:1` | `home = C:\Users\X1C\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none`（`ls` 该路径：不存在；当前用户是 `admin`） |
| `.venv/Scripts/python.exe -c ...` | `error: uv trampoline failed to spawn Python child process / Caused by: entity not found (os error 2)` |
| `run.bat:24,26` | `if exist ".venv\Scripts\python.exe" set VENV=%~dp0.venv`，随后只判断同一路径是否存在 → 选中坏 venv，不回落 `%LOCALAPPDATA%\OCRMiniTool\venv`（`:18`），也**不重建** |
| `.venv/Lib/site-packages` | 52 个 dist-info，含 `pyinstaller-6.22.3`、`torch-2.8.0+cpu`、`torchvision-0.23.0+cpu`、`transformers-4.52.1`、`pywebview-6.2.1` |

**影响**：新同学/CI 跑 `run.bat` 得到与代码无关的 trampoline 报错（且 `:46` 直接执行、无 `|| goto :fail`，失败也只是 `pause` 后 `exit /b 0`，`:46-49`——「启动失败」与「正常退出」在退出码上不可区分），排查成本高；同时该 venv 是**唯一记录了 v1.2.0 实测依赖组合**的物证（见 §3.2）。
**建议**：`run.bat` 增加「能跑起来才算数」探测（`"%VENV%\Scripts\python.exe" -c "import sys" >nul 2>&1`，失败则删除/重建或回落 LOCALAPPDATA）＋ `set PYTHONUTF8=1`（见 D8）；本地手工修复 `rm -rf .venv && uv venv --python 3.12 .venv`。

### 1.4 基线缺失项（HEAD 事实）

`git ls-files` 共 35 个文件：无 `.github/`、无 `tests/`、无 `pyproject.toml`、无 `requirements-lock.txt`、无 `CHANGELOG`、无 `.editorconfig`、无 `.gitattributes`（`tests/`+`tools/` 已于探查后由 `b33cab2` 补齐；`.gitattributes` 由 `afdc03e` 补齐）。
另：本地标签停在 `v1.1.2`，`v1.2.0` / `runtime-torch2.8.0` 只存在于远端（`git ls-remote --tags origin` 可见）——本地 stale，`git fetch --tags` 即可，非仓库缺陷。

---

## 2. 构建链路

### 2.1 `OCRMiniTool.spec`：现状基本合理（事实）

| 事实 | 位置 |
|---|---|
| onedir：`EXE(exclude_binaries=True)` + `COLLECT` | `OCRMiniTool.spec:101-130` |
| 安装包**不含** torch/torchvision：`excludes=[...]` | `:95` |
| 构建预检：venv 里 `find_spec("torch")` 为真则 `SystemExit`（可用 `OCRMIT_ALLOW_BUNDLED_TORCH=1` 绕过） | `:31-36` |
| webview/ → `webview_assets`；icon.ico → `.`（与 `i18n.py:27`、`app_webview.py:169-176` 的查找逻辑对齐） | `:38-41` |
| 运行时钩子 `runtime_hook_utf8.py` | `:91` |
| torch 组件用到的 stdlib 以清单补齐（该做法的必要性有注释与历史翻车记录） | `:58-81` |
| `transformers.models` 全量子模块（`collect_submodules`） | `:57` |
| 关闭 UPX（torch DLL 场景正确取舍） | `:110,126` |

### 2.2 构建链路缺口

| # | 证据 | 结论 | 影响/风险 | 修复要点 |
|---|---|---|---|---|
| B1 | `grep version= OCRMiniTool.spec` 无命中 | 事实：EXE **无 Windows 版本资源** | 资源管理器/杀软/升级比对都看不到 `1.2.0`；只能靠文件名 | 生成 `version_info.txt`（version.py 派生）→ `EXE(version=...)`（D15） |
| B2 | `.venv` 里 `pyinstaller-6.22.3`；`requirements.txt` 无 pyinstaller；`README.md:146` `pip install pyinstaller` 不钉版本 | 事实：构建工具未锁定 | PyInstaller 大版本升级会改 spec 语义（如 COLLECT 参数），构建结果漂移 | 新增 `requirements-build.txt`：`pyinstaller==6.22.3`（D2/D3） |
| B3 | `README.md:147` 无 `--clean` | 事实：构建命令未清缓存 | 旧 `build/` 残留可能进包（onedir 尤其容易「多带」已删模块） | 构建脚本统一 `--clean --noconfirm`（D3） |
| B4 | `ocr_engine.py:360 import fitz`；spec 只 `collect_all('pymupdf')`（`:51-53`） | 推断：`fitz`/`_mupdf.pyd` 靠 PyInstaller import 图顺带收集，未显式验证 | PDF 路径在发行包内 `ModuleNotFoundError` 是典型翻车；必须实机打包验证一次 | 发行前冒烟：打包后跑一张 PDF + 一次图片识别 |
| B5 | `.venv` 装了 `torch-2.8.0+cpu`，而预检 `:32` 见到 torch 就退出 | 事实：**用 `requirements.txt` 建出的 venv 不能直接打包** | 每次发版要么另建 torch-free 环境，要么记得加环境变量；README 完全没写 | 构建脚本自动建独立 build venv（D3），README 写明前置条件（D11） |
| B6 | 预检只挡 torch，未挡 `transformers` 是否可导入/版本是否匹配 | 推断：`transformers==4.52.1` 若被升级，构建不报错、运行期 `GenerationMixin` 占位化 | 高影响、低概率（有 requirements 约束但不锁） | 预检里加 `transformers.__version__ == 4.52.1` 断言（D3） |

### 2.3 `build_components.py`：功能对，可复现性不足

| # | 证据 | 结论 | 影响/风险 |
|---|---|---|---|
| C1 | `:55-56` `uv pip install --python sys.executable --target stage torch==… torchvision==…` | 事实：只钉 torch/torchvision，**传递依赖浮动**（sympy/networkx/jinja2/filelock/fsspec/mpmath/typing_extensions 由构建当天解析） | 同一 tag 两次构建 sha256/size 不同；用户装到的组件依赖组合不可复现 |
| C2 | `:32-33` `TORCH_VER/TV_VER` 与 `downloader.py:244-245` 双写，注释「改动时两处同步」`：29` | 事实：版本号双源 | 漏改一处 → 下载端拼错 URL 或标记判定永远「未安装」 |
| C3 | `:69-77` `zipfile.ZIP_DEFLATED`，`zf.write(p, arcname)` 带文件 mtime | 推断：zip 内时间戳随构建时刻变化 | 即便依赖一致，sha256 也不会相同；无法「重建校验」 |
| C4 | `:94-107` 写 `dist_components/manifest.json`（size/sha256）；GitHub API 资产列表无 manifest | 事实：manifest **从未发布** | 应用端拿不到哈希清单（与 §4.4 断链互为因果） |
| C5 | `:67-78` 只产出整包 `ocrmini-runtime-torch2.8.0-cu128.zip`（≈3.3GB）；`downloader.py:508` 需要 `…zip.part01-of-2/.part02-of-2` | 事实：**脚本不分卷**，分卷是发布时手工切片（实测 part01=1,992,294,400 B=1900 MiB 整，part02=1,507,090,737 B 为余数） | 发布流程每次靠手工/记忆；分卷边界不在任何文件里，下载端也无法校验 |
| C6 | `:5` 注释示例 `runtime-torch2.11.0` | 事实：文档漂移（实际 2.8.0） | 误导后续维护者 |
| C7 | `:59-62` 收集 stage **全部顶层目录**（含 numpy/pillow/typing_extensions 等纯 Python 依赖） | 推断：`runtime/` 被 `sys.path.insert(0)`（`ocr_engine.py:101`）后，这些副本会**遮蔽**冻结包 `_internal` 内的同名包 | 版本混用（如 PIL/numpy）→ 难诊断的 ABI/行为差异；建议 manifest 记录 `tops` 并做允许清单断言 |

**建议**：`build_components.py` 增 `--split 1900MiB`、manifest 增 `parts[{name,size,sha256}]` + `tops`、传递依赖用 `--no-deps` + 显式钉版本（或锁文件），并断言 `py -3.12 64bit` 平台，见 D7。

---

## 3. 依赖管理

### 3.1 版本口径矩阵（同一条事实，五处写法）

| 位置 | 写法 | 与「可用组合」的关系 |
|---|---|---|
| `requirements.txt:5`（注释） | 上游验证：**torch 2.13** / transformers 4.52.1 / hf-hub 0.36.0 | 与全仓代码不符（`ocr_engine.py:9` 同样写 2.13） |
| `requirements.txt:20-21` | `torch>=2.5`、`torchvision>=0.20` | 无上限：新装机会拿到 2.9+/2.10+/2.13 |
| `build_components.py:30-31` | 「torch 必须 <2.9：2.10+ 移除 `_model_output_flatten`」 | 与下一行互相矛盾 |
| `downloader.py:240-242` | 「torch 2.11 起移除 …；2.8.0+cu128 是 AutoDL 真机验证组合」 | 阈值又写成 2.11 |
| 实际钉死值 | `downloader.py:244` / `build_components.py:32` = **2.8.0**；`requirements.txt:24` `transformers==4.52.1` | 唯一有实测背书的是 2.8.0 |
| `README.md:166-169` | `pip install torch torchvision --index-url …/cu128 --force-reinstall`（不钉版本） | **照做即装最新版 → 按自家注释必然破坏 transformers 4.52.1** |

**影响**：这是本报告最可能导致「用户排查一整天」的一条：源码用户按 README 装 CUDA，得到 2.9/2.10+，报 `None of PyTorch … have been found` / `NameError: _model_output_flatten`，而文档说这套组合「验证过」。
**建议**：`requirements.txt` 收紧为 `torch>=2.5,<2.9`、`torchvision>=0.20,<0.24`；README 命令补 `torch==2.8.0 torchvision==0.23.0`；统一把「2.10+ 移除」的阈值写成一处（推荐 `<2.9` 的整句），删掉 `torch 2.13` 字样（D2/D11）。

### 3.2 已实测组合（来自损坏 venv 的 dist-info，可直接落成锁文件）

`torch 2.8.0+cpu` / `torchvision 0.23.0+cpu` / `transformers 4.52.1` / `tokenizers 0.21.4` / `huggingface_hub 0.36.2` / `numpy 2.5.4` / `safetensors 0.8.0` / `sympy 1.14.0` / `networkx 3.7` / `jinja2 3.1.6` / `filelock 4.2.4` / `fsspec 2026.9.0` / `pywebview 6.2.1` / `pythonnet 3.1.0` / `clr_loader 0.3.1` / `pymupdf 2.0` / `pyinstaller 6.22.3` / `pillow 13.0.0` / `certifi 2026.10.23`（共 52 个）。
**对照**：STT 用 `requirements.txt`（下限）+ `requirements-lock.txt`（`==` 快照，36 行）双文件，并在 CI 里 `pip install -r requirements-lock.txt` —— 同一作者已有现成范式。

### 3.3 缺锁文件的具体影响

| 影响面 | 现状 | 建议 |
|---|---|---|
| 复现构建 | 无 lock；`build_components` 传递依赖浮动 | `requirements-lock.txt` + `requirements-build.txt`（D2） |
| 多人/多机 | `run.bat` 每次按 `requirements.txt` 现解析（`:38,41`） | 首次安装后 `uv pip freeze > requirements-lock.txt` |
| 可选依赖混在必需里 | `pymupdf/send2trash/certifi` 与 torch 同文件 | 拆 runtime / optional / dev / build 四份 |
| 注释漂移 | `requirements.txt:5` 的 2.13 | 只保留一处「可用组合」并附实测日期 |

---

## 4. 分发

### 4.1 版本号「单一事实来源」只在代码里成立，发布链路没接上

| 事实 | 位置 |
|---|---|
| 版本单一来源声明 | `version.py:1-3`（`APP_VERSION="1.2.0"`），经 `webview_backend.py:33` 暴露到 UI（`:224 "version": APP_VERSION`） |
| README 手写发行包名 | `README.md:149` `OCRMiniTool-v1.1.2-windows-x64.zip` —— **落后一个版本**（v1.1.2 vs 1.2.0），且第 147 行构建命令不读 version.py |
| 主 zip 内/文件名无机器可读版本来源 | 无 VERSION 文件、EXE 无版本资源（B1） |
| 体积口径 | `README.md:28` 「安装包 ~300MB 级」；实际 v1.2.0 资产 **151,387,757 B ≈ 144 MB**（GitHub API） |
| 系统需求 | `README.md:211` 「硬盘 8 GB」；6.8GB 模型 + 3.3GB CUDA 组件 + 程序 → 实际需 ≥12GB |

### 4.2 发布流程：全手工、无脚本

事实：`git ls-files` 无 `build.bat`/`release*`/`make_*zip*`；README `:143-150` 是 3 条手工命令（含手写 `Compress-Archive` 和手写版本号）。对照 STT 有 `build_webview.bat` 等入库脚本。
缺失环节：构建 → 改名 → 压缩 → 算 SHA256 → 打 tag → 传 Release → 写 Release 说明，全部靠人；**没有任何一步可被机器校验**。
建议：`build.bat`（或 `tools/build.py`）一条命令产出 `dist\OCRMiniTool-v<version>-windows-x64.zip` + `SHA256SUMS.txt`，并在打 tag 前校验 `git status --porcelain` 为空（D3/D4）。

### 4.3 可溯源性问题（时间线实测）

| 事件 | 时间（+08） | 证据 |
|---|---|---|
| 版本提交 `4bf8165 chore: 版本 1.2.0` | 14:02:12 | `git log --format='%h %ci %s'` |
| `cd77339 fix(downloader): cu128 改分卷发布` | 14:21:15 | 同上 |
| 分卷资产上传（part01/02） | 14:24–14:32 | GitHub API `created_at` 06:24–06:32Z |
| v1.2.0 主 zip 上传 | 14:55:34 | API `created_at` 06:55:34Z |
| `ecd717a fix(ui) …同口径`（HEAD） | 14:55:54 | `git log` |
| 两个发布 tag 指向 | 均为 `4bf8165` | `git ls-remote --tags origin` |

**结论**：`v1.2.0` 与 `runtime-torch2.8.0` 两个 tag 都指向 **早于** 资产上传的 `4bf8165`；主 zip 上传时间比 HEAD（`ecd717a`）提交早 20 秒，说明发布包不可能包含该修复，也无法由任何 tag 唯一确定其源码状态（当时工作区正是 §1.1 的 CRLF 脏状态）。任何人 `git checkout v1.2.0` 都重建不出发出去的包。
**建议**：发布脚本强制 `HEAD == tag` 且工作区干净；`runtime-torch*` tag 单独打在改 `build_components.py` 的提交上（D4）。

### 4.4 完整性链条：发布侧给了哈希，应用侧不校验

| 环节 | 事实 |
|---|---|
| 发布侧有哈希 | Release body 明写 CPU `69ec27ad…`（与 API asset digest 一致）、cu128 整包 `6f10c1b7…`；`build_components.py:101-104` 也算了 sha256 |
| 应用侧不校验 | `downloader.py:188` 只读 `Content-Length`；`:213-223` 只在字节数不符时删残档；`grep sha256` 仅命中 `:238,281` 两处注释 → **代码路径无 hashlib** |
| manifest 不发布 | §2.3 C4 |
| 主 zip 无哈希 | v1.2.0 Release body 无 SHA256（对比 v1.0.0 body 有）——**回归** |

**影响**：镜像/代理劫持、CDN 截断、发布方重切片（见 4.5）都会以「zip CRC 错误」「torch 导入失败」等面目出现，用户/维护者拿不到「是哪一段坏了」的信息。3.3GB 重下代价高。
**建议**：两阶段——先 warn（下载后算 sha256，与发布清单比对，不一致只告警）→ 稳定后 enforce（不一致即删除并报「组件包校验失败」）。见 D5。

### 4.5 分卷续传缺陷（有日志实录，属可复现 bug）

代码事实：分卷直接写最终名（`downloader.py:509 pdest = RUNTIME_DIR / pname`），`pname` 就是 `.part01-of-2`；下次运行 `:510 if not pdest.exists()` 只按**存在**跳过，不看大小/哈希；`:170-174` 把 HTTP 416 一律当「已完整」。
后果（推断链）：下载中取消/进程被杀 → 留下短文件 → 下次视为完整 → 拼接（`:524-539`）得到长度错或内容错的 zip → 解压报 CRC/坏文件，且**不会自愈**（需手工删 `runtime/*.part01-of-2`）。
日志佐证：`logs/app-202611.log` 14:14:53 part01=1437.3MB OK、14:16:33 part02=1437.3MB OK；14:19:38 part01=**1900.0MB** OK、14:19:42 part02=1437.3MB OK → 发布方在两个时间点用了不同切片，旧 part 被复用后又被覆盖，正是上述路径。
**建议**：`_TORCH_FLAVORS` 写死每卷期望字节数（来自 manifest），存在但大小不符 → 删后重下；或统一用 `.partN.part` 暂存、完成再改名。见 D6。

### 4.6 免安装目录结构 / MOTW / 签名

| 项 | 事实 | 评价 |
|---|---|---|
| 目录结构 | zip 根含 `OCRMiniTool\`（exe + `_internal\`）；`models/ outputs/ logs/ settings.json runtime/` 均落 exe 旁 | 免安装成立，但**没有可写性探测/回落**（`webview_backend.py:44` import 期 `OUTPUT_DIR.mkdir` 失败即崩，`:37-43`；`downloader.py:264` runtime 同理）→ 推断：放 Program Files 或只读共享盘会「双击无反应」 |
| 升级覆盖 | 无 VERSION 标记、无「先删旧目录」指引 | 推断：onedir 原地解压会残留旧 `_internal` 文件，出现新旧混跑 |
| MOTW | `motw_unblock.py:1-34` 启动即删 Zone.Identifier，设计正确、注释清楚，`app_webview.py` 中在 clr 载入前调用 | 事实：已针对 zip 分发解好 |
| 代码签名 | `OCRMiniTool.spec:115 codesign_identity=None`，README/发布无签名步骤 | 事实：未签名 → SmartScreen 必弹；短期可接受 |
| UPX | 全关（`:110,126`） | 事实：对 torch DLL 正确（避免杀软误报/加载失败） |

### 4.7 组件下载只有 GitHub 一条路

`downloader.py:249-252`：`_TORCH_REL_BASE` 默认 GitHub Release，仅可用环境变量 `OCRMIT_RUNTIME_BASE` 覆盖（面向测试/内网），**不是用户可设置项**；而模型下载有镜像设置（`downloader.py:73-89`、`README.md:83`）。目标用户群以 zh-CN 为主（默认中文、linux.do），3.3GB 从 GitHub 直连失败率高，且失败后只有「再点一次」。
**建议**：把「组件下载基址」纳入设置（或至少 README 给出镜像/代理解法），并在 UI 显示「组件源：GitHub」。风险：中（涉及下载可信性，建议只做可配置不改默认）。

### 4.8 无更新检查 / 无自动更新

事实：全仓 `grep check_update|releases/latest` 无命中；对照 STT `webview_backend.py:678 check_update()` + `version.py:317-318 GITHUB_RELEASES_PAGE` + `tests/test_version_compare.py`。
影响：v1.2.0 用户不知道有新修复；作者每次发版只能靠 README/Release 流量。
**建议**：只做「检查更新 → 显示版本 + 跳转下载页」（半自动），不做静默自替换（见 §8 不建议）。

---

## 5. 升级与迁移（向前兼容）

| # | 事实/证据 | 风险 | 建议 |
|---|---|---|---|
| M1 | `settings.json` **无 schema/版本字段**（`webview_backend.py:46-56 DEFAULT_SETTINGS` 仅 9 个键，无 `settingsVersion`） | 中：将来删键/改语义无迁移入口，只能靠默认值兜底 | 加 `settingsVersion: 1` + `_migrate(data)` 顺序升级（D13） |
| M2 | 损坏即静默清空：`settings_store.py:44-50` 解析失败 `return {}`，`.tmp` 原子写不保留备份（`:76-88`）；下次一次写就把原文件覆盖 | 中：一次手改 JSON 打错括号 → 镜像/模型目录/语言全丢，无提示、不可恢复 | 解析失败时把原文件改名 `settings.json.corrupt-<ts>`，并回退 `.bak`（D13） |
| M3 | 改模型目录只切引擎指针（`webview_backend.py:175-182`），不迁移已下载的 6.8GB | 中低：用户改目录后 UI 显示「未下载」，重下 6.8GB | 切换时提示「旧目录有 N GB 模型，是否移动/复制」或至少告知仍需下载 |
| M4 | v1.1.2→v1.2.0 布局变化（包内 torch → 外部 `runtime/`）由标记文件兜底：`downloader.py:294-319` 标记+关键文件检查，`ocr_engine.py:89-124` 以标记为准插路径 | 低：设计合理（标记最后写，避免「有标记无文件」） | 保持；补一条「标记存在但 torch 导入失败」的 UI 提示（已有 `ocr_engine.py:176-179 log_error`） |
| M5 | 卸载/清理：`runtime/`、`models/`、`outputs/`、`logs/` 均无清理入口；README 只说删文件夹 | 低 | README 加「彻底卸载」小节 |

---

## 6. 工程规范缺口

| # | 事实 | 影响 | 对照 STT | 建议 |
|---|---|---|---|---|
| E1 | 无 CI（无 `.github/`） | 所有回归靠手工；本次 CRLF 事故本可被 CI 一眼发现 | `.github/workflows/ci.yml`：ruff + mypy + 双平台 unittest | 抄一份，去掉模型相关步骤（D9） |
| E2 | 无 lint/formatter 配置（无 pyproject） | 风格漂移，长文件继续膨胀（`webview_backend.py` 47KB） | `pyproject.toml`：ruff 只开 `E9,F63,F7,F821,F811`；mypy 只覆盖纯逻辑层 | 同款分级治理，先不引入风格类（D10） |
| E3 | 无测试（HEAD；探查后 `b33cab2` 已补 16 个测试文件 / 101 用例 + `tools/check.py` 静态体检，但**仍无 CI 自动跑**） | `downloader` 的 Content-Length/分卷/镜像改写等纯逻辑当时零覆盖——本次两个最严重缺陷都出在这里 | 16 个 `tests/test_*.py`，含 `test_downloader_integrity.py`、`test_multipart.py`、`test_settings_store.py` | 新补的 `test_downloader.py` 与 `tools/check.py` 应接入 CI（D9） |
| E4 | 日志无轮转：`applog.py:6,46-48` 只按月分文件、追加写，无大小上限/保留期；实测 `logs/app-202611.log` 13KB（约 12 天） | 低（增长慢）但无界：`log_error` 会写完整 traceback；长期用户 `logs/` 只增不减 | STT 同样按月（对照即可） | 单文件 >8MB 轮转 + 保留 6 个月（D12） |
| E5 | `runtime_hook_utf8.py:10` 在 runtime hook 里 `os.environ["PYTHONUTF8"]="1"`，注释称「须在解释器初始化前生效」——但 runtime hook 在解释器初始化**之后**执行；`app_webview.py:365` 同理 | 中低：UTF-8 模式实际未开启（实测：运行中设 `PYTHONUTF8` 后 `sys.flags.utf8_mode=0`，`-X utf8` 才为 1，默认编码仍是 `cp936`）；第三方库无 `encoding=` 的 `open()` 仍有风险，自有代码基本都显式 UTF-8 | STT 同款钩子 | 启动器里 `set PYTHONUTF8=1` 后再起进程；冻结版无法从内部开启 → 修正注释或改由外层 bat/快捷方式设置（D8） |
| E6 | 无 CHANGELOG；`version.py` 只有 2 个常量 | 发布说明靠手写 Release body，历史容易丢 | STT `version.py` 内嵌逐版变更摘要（317+ 行） | 可选：`docs/CHANGELOG.md` 或沿用 STT 风格 |
| E7 | 注释性漂移：`i18n.py:11-12` 仍写「frozen（PyInstaller onefile）」；`downloader.py:30` UA 写死 `OCRMiniTool/1.0` | 低：误导维护者/服务端统计 | — | 随 §D11 一并清理 |

---

## 7. 低风险可落地清单

| 编号 | 改动点 | 收益 | 风险 | 验证 |
|---|---|---|---|---|
| **D1** | ✅ **已落地**（`afdc03e`，实现与建议一致）：`.gitattributes` 统一 LF、`*.bat/*.cmd` 保 CRLF、二进制清单 + `tools/normalize_eol.py` 一次性归一 | 立刻消掉 20 文件全量脏；review 回到「只看到真实改动」；跨机不再反复翻脏 | 低（已用 `run.bat` 验证 w/crlf 保留） | `git status` 空；`git ls-files --eol` 文本全 `w/lf`（`.bat` 例外）；`git diff HEAD --stat` 无代码差异 |
| **D2** | 拆分依赖：`requirements.txt`（运行时下限，torch 改 `>=2.5,<2.9`、torchvision `<0.24`）、`requirements-lock.txt`（由 §3.2 的 52 个版本生成 `==`）、`requirements-build.txt`（`pyinstaller==6.22.3`）；删掉「torch 2.13」字样，阈值统一写 `<2.9` | 源码用户不再按 README 装出坏组合；构建可复现；CI 可复用锁文件 | 低（只加文件+收紧上限；锁文件与当前 venv 一致已实测） | 全新 venv `uv pip install -r requirements-lock.txt`，`python -c "import torch,transformers;print(torch.__version__,transformers.__version__)"` → `2.8.0+… 4.52.1` |
| **D3** | 新增 `build.bat`（或 `tools/build.py`）：校验工作区干净 → 用 **torch-free** venv 装 `requirements-build.txt` → `pyinstaller --clean --noconfirm --distpath dist --workpath build OCRMiniTool.spec` → 从 `version.py` 取版本号压缩并算 SHA256；spec 预检再加 `transformers.__version__=="4.52.1"` 断言 | 发布从 6 步手工变 1 条命令；消除「带 torch 环境打包被拒」「版本号手写」两类事故 | 中（需在真机构建一次验证；脚本本身只是既有命令的封装） | 本机不可跑（无构建环境）→ 交给有 Windows 构建机的人执行，产物名/哈希与 Release 一致 |
| **D4** | 发布前检查脚本/清单：`git fetch --tags` + 确认 `HEAD==tag`、`git status` 干净、两个 tag（`v*` 与 `runtime-torch*`）分别打在正确提交；Release body 自动附 `SHA256SUMS` | 资产 ↔ 源码可溯源；修复 §4.3 的「包里有未提交改动」 | 低（流程约束） | 对 v1.2.0 重跑检查应报错（复现 §4.3），对未来发布应为绿 |
| **D5** | `downloader` 增组件包 sha256 校验（两阶段）：先发布 `manifest.json` 或 `<zip>.sha256` 资产，代码默认 **warn-only**；稳定后改严格（不一致删除并提示重下） | 3.3GB 下载可判定完整性；镜像/劫持/重切片都可定位；与已发布的 Release body 哈希闭环 | 中低（第一阶段只告警，不影响现有安装） | 单测：本地 HTTP 服务器喂错内容 → 断言 warn 日志/（严格模式下）删除；用真实发布哈希对账 cpu/cu128 |
| **D6** | 修分卷续传：`_TORCH_FLAVORS` 记录每卷期望字节数（或改 `.partN.part` 暂存、完成再改名）；`if not pdest.exists()` 改为「存在且大小==期望」；416 时校验大小 | 消除「取消后永久坏包、只能手工删」的坑（§4.5 有日志实录） | 低（纯下载层逻辑，可单测） | 单测：造短 part 文件 → 再跑 `download_torch` → 断言重新下载并拼接成功 |
| **D7** | `build_components.py`：`--split 1900` 产出 `.part01-of-2/.part02-of-2`；manifest 增 `parts[{name,size,sha256}]` 与 `tops`；传递依赖改 `--no-deps` + 显式钉版本（或锁文件）；`torch` 版本从单一常量读 | 分卷/哈希/依赖全部由脚本产出，去掉手工切片；解压后不再意外遮蔽冻结包同名依赖 | 中（需重跑一次构建验证体积/内容） | 构建后：`unzip -l` 顶层与 manifest `tops` 一致；两卷 sha256 入 manifest；下载端按 manifest 校验通过 |
| **D8** | `run.bat`：`set PYTHONUTF8=1`（`:45` 前）；把 `.venv` 探测改为「执行 `-c "import sys"` 成功才使用」，失败则提示并回落 `%LOCALAPPDATA%` 或删除重建 | 修好本节最直观的「新人第一天就卡住」问题；真开 UTF-8 模式 | 低 | 删掉/保留坏 `.venv` 各跑一次 `run.bat`：坏 venv 下应提示并回落，不出现 trampoline 报错 |
| **D9** | 抄 STT `.github/workflows/ci.yml`：`unittest discover`（windows+ubuntu）+ ruff + mypy；先只跑纯逻辑测试（不下载模型/不建 WebView）。注：测试套件已由 `b33cab2` 落地（`tests/run_tests.py`），**缺的是 CI 编排** | 回归门禁；CRLF/语法/未定义名类问题在 PR 阶段挡住 | 低（可为 `workflow_dispatch` 起步） | push 后 CI 全绿；故意提交一个未定义名 → ruff 报 F821 |
| **D10** | 新增 `pyproject.toml`（仅 `[tool.*]`）：ruff `select=["E9","F63","F7","F821","F811"]`、`extend-exclude` 掉 `build/dist/runtime/models/logs`；mypy 先覆盖 `settings_store.py`、`downloader.py`（纯逻辑部分） | 与 STT 同款分级治理，不淹没真实错误 | 低 | `ruff check .`、`mypy` 本地跑通 |
| **D11** | README/注释一次性对齐：`:149` 版本号改用 `<version>` 或脚本生成；`:28` 300MB→~145MB；`:211` 硬盘 8GB→12GB；构建章节补「必须 torch-free 环境 + `OCRMIT_ALLOW_BUNDLED_TORCH` 语义 + 分卷/上传步骤」；`i18n.py:11` onefile→onedir；`downloader.py:30` UA 用 `APP_VERSION` | 消除用户/维护者的错误前提 | 低 | 通读校对；可加 `tools` 脚本 grep 硬编码 `v\d+\.\d+\.\d+` 告警 |
| **D12** | `applog.py` 加轮转：单文件 >8MB 时改名为 `app-YYYYMM.1.log`（最多 3 份），写入前检查，失败静默跳过；另加保留 6 个月清理 | 长期用户日志不无界增长 | 低（日志路径已有「永不抛异常」约定） | 临时把上限调 1KB，连续写 → 断言出现 `.1.log` 且不超份数 |
| **D13** | `settings_store`：解析失败时把坏文件改名 `settings.json.corrupt-<ts>`；写入后保留 `settings.json.bak`；`DEFAULT_SETTINGS` 增 `settingsVersion` 并在 `read()` 里顺序迁移 | 设置不再「一次手误全丢」；为将来删/改键留迁移入口 | 低—中（迁移函数需单测） | 写入坏 JSON → 启动后 `.corrupt-*` 存在且 UI 用默认值；写新键 → `settingsVersion` 递增 |
| **D14** | `.gitignore` 补 `*.zip`、`_internal/`、`Thumbs.db`、`desktop.ini`、`.ruff_cache/`、`.pytest_cache/`、`.mypy_cache/`、`.vscode/`；`settings.json` 改 `/settings.json` | 噪音/误提交风险下降；不再误伤子目录同名文件 | 低 | `git check-ignore -v` 逐项复测（如 §1.2 的探针） |
| **D15** | spec 增 Windows 版本资源：由 `version.py` 生成 `version_info.txt`（FileVersion/ProductVersion=1.2.0、CompanyName、ProductName）→ `EXE(version=...)`；可选：仿 STT 加只读 `check_update`（拉 `releases/latest` + `_ver_newer`，只提示+跳转） | EXE 属性可见版本（杀软/升级/用户支持都有用）；用户能发现新版本 | 低（版本资源）/中（网络检查需容错） | `pyinstaller` 后右键 EXE→属性→详细信息可见 1.2.0；`check_update` 断网时应静默失败 |

---

## 8. 不建议做

| 不建议 | 理由 |
|---|---|
| 把 torch/torchvision 重新打进安装包（含 CPU 版） | issue #3 已定案：内置 CPU 版会让 N 卡用户永远检测不到 GPU；包体从 145MB 回到 4.4GB，且 `spec:31-36` 预检就是为拦这个 |
| 做「静默下载并替换 exe」的全自动更新 | 无代码签名（`spec:115`）、主 zip 无哈希（§4.4）、onedir 有残留文件问题——自动替换会把「下载/校验/权限」三类失败直接变成启动失败。只做「检查更新 + 跳转下载」 |
| 为体积启用 UPX 或 strip | torch DLL/`.pyd` 被压缩后杀软误报率与加载失败率上升；当前全关（`:110,126`）是正确取舍 |
| 回归 onefile | onefile 每次启动解压数百 MB 到 `%TEMP%\_MEIxxxx`（`spec:4-7`、STT v2.2.9 结论），启动慢、占 C 盘、强杀留垃圾 |
| 引入 pytest/框架式重构、或把项目改成可 `pip install` 的包 | 与 STT 现实不符（stdlib unittest + ruff/mypy 已够），当前只需补门禁，不需要重构税收 |
| 在无 Windows 构建机可验证的情况下先动「打包布局/签名流水线」 | 本机 `.venv` 损坏、不能 `pip install`/`pyinstaller`；B4（fitz/pymupdf）、B6、D3 的结论必须有一次真机打包冒烟才能定稿 |

---

## 9. 证据复现 / 未验证清单

**一条命令可复现的关键证据**

```bash
cd OCRMiniTool
git status --porcelain | wc -l                     # 20
git diff --numstat                                 # 每文件 +N/-N 相同
git ls-files --eol                                 # i/lf w/crlf / w/mixed
git config --get core.autocrlf                     # 空
git ls-remote --tags origin                        # v1.2.0 = runtime-torch2.8.0 = 4bf8165
git log -3 --format='%h %ci %s'
cat .venv/pyvenv.cfg                               # home=C:\Users\X1C\...
.venv/Scripts/python.exe -c "import sys"           # uv trampoline failed ...
```

```bash
# UTF-8 模式实测（证明 runtime hook 里设 PYTHONUTF8 不会打开 UTF-8 模式）
python -c "import os,sys,locale; os.environ['PYTHONUTF8']='1'; print(sys.flags.utf8_mode, locale.getpreferredencoding(False))"   # 0 cp936
python -X utf8 -c "import sys,locale; print(sys.flags.utf8_mode, locale.getpreferredencoding(False))"                            # 1 utf-8
```

**未验证（本机条件不足，需真机/联网构建机）**

1. PyInstaller 实际产物是否缺 `fitz`/`_mupdf.pyd`、`safetensors`、`huggingface_hub`（B4，纯推断）。
2. `runtime/` 遮蔽 `_internal` 同名依赖（C7）的真实影响——需打一次包、分别装 cpu/cu128 组件做识别冒烟。
3. `build_components.py` 全流程（3.3GB 下载/切片/上传）与 PyPI/uv 解析结果。
4. Release 资产 hash 与「本地重建」是否一致（C3 推断为不一致）。
