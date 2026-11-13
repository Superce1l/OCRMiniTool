# v1.6.0 — 可配置输出目录、批量合并导出、EXE 版本资源、发布前检查

> 本版补齐「输出放在哪、批量结果怎么带走、发版怎么核」三件事；
> 不改模型、不加依赖。状态：**代码已就绪，未打包/未发布**。

## 亮点

- **可配置输出目录**：设置页新增「输出目录」——识别结果自动落盘、「最近识别」列表、
  历史读取/删除、「打开输出文件夹」全部跟随；留空仍是程序目录下的 `outputs`。
  目录必须绝对路径且可写：不可创建/不可写会直接提示并**不落盘**（同一次设置里的
  其它改动也一并放弃），识别/下载进行中不允许改（避免结果分裂到两个目录）。
- **批量合并导出**：批量结果面板新增「导出全部（合并）」——只取成功项，按
  `# 文件名` 小标题与 `---` 分隔拼成一份 Markdown，文件名 `<首个成功项>-全部.md`；
  全失败/空结果时提示且不产文件。
- **EXE 版本资源（D15）**：`build_exe.py` 由 `version.py` 生成 PyInstaller 的
  `VSVersionInfo`，构建时写到 `build/OCRMiniTool_version_info.txt`，spec 动态传给
  `EXE(version=...)`——打包后 EXE 属性里能看到 FileVersion / ProductVersion，
  版本号仍然只有一个来源。
- **发布前检查（D4）**：新增 `tools/release_check.py`，五项检查逐条 ✓/✗：
  `version.py` ↔ tag、工作区干净、tag 存在且指向 HEAD、远端有该 tag（可 `--offline`）、
  zip 名含版本并计算 sha256（`--write-sums` 生成 `SHA256SUMS.txt`）；
  退出码 0/1/2。发版从「靠记忆」变成「跑一条命令」。

## 行为变化（升级前请知悉）

| 变化 | 说明 |
|---|---|
| 新设置键 | `outputDir`（默认 `""` = 程序目录 `outputs`）；旧 `settings.json` 自动补齐 |
| 输出目录切换 | 只影响之后的结果与历史视图，**不移动已有文件**；运行中改会被拒绝（`err.busy`） |
| 导出全部 | 仅当前批次成功项，合并文件只做浏览器下载，不写入输出目录 |
| 历史列表 | 仍只列 `.md` / `.txt`（JSON 结果请在输出目录里查看），避免「列得出但读不了」 |

## 已知限制 / 待真机确认

- **EXE 版本资源尚未在真实产物上抽查**：本批只验证了 `VSVersionInfo` 文本与 spec 接线
  （未跑 PyInstaller）；首次构建后请用 `(Get-Item .\OCRMiniTool.exe).VersionInfo` 核对
  FileVersion=1.6.0。
- `release_check.py` 的远端 tag 检查需要有 `origin`；离线环境用 `--offline` 跳过。
- 合并导出为纯前端拼接：不写输出目录、文件名固定中文后缀 `-全部.md`。
- 仍未做：docx/xlsx/可搜索 PDF/图表裁剪（E8/E9/E10/E15）、多 GPU/cu129 组件档、
  组件分卷/钉版本的构建脚本改造（D7）；打包版仍无 CLI。

## 验证

- `tests/run_tests.py` → **807/807** 通过（新增 51 条：后端 16 + 前端 15 + 构建 20）
- `tools/check.py` → 0 错误 0 告警；`node tools/frontend_sanitize_check.js` 22+20 全过
- headless Edge（真实 Chromium 内核）CDP → **17/17**（输出目录写回/status 跟随/非法值回滚、
  合并导出内容与文件名、失败项排除、全失败隐藏）
- 真后端脚本 → **19/19**（输出目录全链路含历史读写与不可写拒绝、版本资源文本、
  `release_check` 退出码矩阵）

## 升级提示

- 直接替换程序目录即可；`models/` / `outputs/` 不受影响。
- 若你之前把结果放在程序目录 `outputs`，继续留空即可；想换位置在设置页填绝对路径。
