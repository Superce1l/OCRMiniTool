# v1.7.0 — 可搜索 PDF 导出、组件构建可复现

> 本版把「扫描件变成可检索 PDF」和「组件包构建可复现」补齐；不改模型、不加依赖
> （可搜索 PDF 用已有的可选依赖 PyMuPDF，未安装时按钮会给出明确提示）。
> 状态：**代码已就绪，未打包/未发布**。

## 亮点

- **可搜索 PDF 导出**：以「合并解析」识别单个 PDF 后，工具栏出现「导出可搜索 PDF」——
  在**原 PDF 页面**上叠加不可见文本层（`render_mode=3`），文字可搜索、可复制、可选中，
  页面外观不变。中文用 PyMuPDF 内置 CJK 字体，无需安装 Office 或字体文件。
  文件写入设置里的输出目录（同名递增，不覆盖源 PDF）。
  - CLI 也有：`python cli.py --input scan.pdf --format pdf`（非 PDF 退出码 1、
    缺 blocks/缺 PyMuPDF 退出码 2；`--pages` 会映射回原页）
  - 文本层基于识别结果（含 bbox），**不随前端编辑**；批量模式不提供该导出
- **组件构建可复现（D7）**：
  - `build_components.py` 启动先校验自身 `TORCH_VER/TV_VER` 与 `downloader` 里的常量一致，
    不一致直接退出（版本漂移挡在构建前）
  - `--split MIB` 自动分卷：`<zip>.part01-of-N`（与下载端命名完全一致、N 不补零），
    cu128 默认按 1900 MiB 切；manifest 增加 `parts[{name,size,sha256}]` 与 `tops`，
    `SHA256SUMS` 同时覆盖 zip、parts 与 manifest
  - `--no-deps` 需显式 `--pins "pkg==ver,…"`，避免「依赖解析悄悄换版本」

## 行为变化（升级前请知悉）

| 变化 | 说明 |
|---|---|
| 新按钮 | 「导出可搜索 PDF」仅在「合并解析 + 单个 PDF + 有 blocks」时出现；批量结果不显示 |
| CLI `--format` | 新增 `pdf`；仍需 PyMuPDF（组件/环境里有 pymupdf 即可） |
| 组件发布资产 | cu128 现在由脚本自动切两卷；manifest 新增 `parts/tops`（旧字段不变，下载端兼容） |
| 构建校验 | 组件脚本在 torch/torchvision 常量与 downloader 不一致时拒绝构建 |

## 已知限制

- **文本层是块级近似**：每个版面块按 bbox 放一段文本，多行块若放不下会退化为左上角单行；
  搜索/复制可用，但**选中高亮位置**可能与视觉文字略有偏差（不改变原页面）。
- bbox 坐标按引擎默认 dpi=200 换算；页面旋转/非标准 CropBox 的 PDF 建议先看导出的可检索效果。
- 可搜索 PDF 仅覆盖「识别过的页」（`pdfPageRange` 生效时只给选中页写文本层，原 PDF 其他页保持原样）。
- 仍未做：docx/xlsx（E9/E10）、图表区域裁剪（E15）、HTTP API 稳定化（E18）、历史缩略图（E20）、
  cu129 组件档；打包版仍无 CLI、EXE 版本资源需首次构建后抽查（v1.6.0 起已生成）。

## 验证

- `tests/run_tests.py` → **875/875** 通过（新增 68 条：E8 后端 36 + 前端 14 + D7 18）
- `tools/check.py` → 0 错误 0 告警；`node tools/frontend_sanitize_check.js` 22+20 全过
- 真后端脚本 → **27/27**（2 页 PDF 文本层往返、中文抽取、页范围映射、接口导出/同名递增、
  CLI 前置校验、分卷命名/拼接哈希/manifest/版本校验）
- headless Edge CDP → **11/11**（按钮可见性、成功/失败提示、无 pdfAvailable 与批量项隐藏）

## 升级提示

- 直接替换程序目录即可；`settings.json` / `models/` / `outputs/` 不受影响。
- 想用可搜索 PDF：确认推理引擎组件里带 PyMuPDF（CPU/CUDA 组件均已包含）；源码运行时
  `pip install pymupdf` 即可。
