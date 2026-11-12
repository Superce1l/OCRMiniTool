# v1.5.0 — 结构化 JSON 输出、结果内查找替换、组件包 sha256 校验

> 本版把「可编程的结果」与「下载可信度」补齐：**不改模型、不加重依赖**。
> 状态：**代码已就绪，未打包/未发布**（等下一次构建窗口）。

## 亮点

- **结构化 JSON 输出**：每页的版面块都带 `type / label / level / bbox / text / mergePrev`，
  顺序即阅读序；bbox 是模型原始像素坐标。三个出口**完全同构**：
  - GUI：设置「输出格式 = JSON」→ 自动落盘 `outputs/<名字>.json`，保存按钮也导出 `.json`
  - CLI：`--format json` 或 `--json`（`pages[].blocks` 与顶层 `blocks` 两个视图）
  - HTTP：`POST /api/recognize` 响应与批量 item 直接透传 `blocks`
  - 这张门打开后，docx / xlsx / 可搜索 PDF / 图表裁剪都只差下游渲染器
- **结果内查找 / 替换**：原文视图上方新增查找栏——上一个/下一个（精确定位并选中）、
  替换、全部替换；改动走既有编辑回写（「已编辑」标记、批量条目同步），
  多页结果按当前页处理，「全部」视图只读并给出提示。
- **组件包 sha256 校验**：下载（分卷先拼接）完成、解压之前校验整包哈希。
  期望值来源按优先级：内置 `_TORCH_FLAVORS[...]["sha256"]` → 远端 `<zip>.sha256` →
  远端 `manifest.json`。默认 **warn-only**（日志告警、继续安装）；
  `set OCRMT_VERIFY_RUNTIME=strict` 时不一致会删除产物并报错。
  `build_components.py` 现在同时产出 `<zip>.sha256` 与 `SHA256SUMS`，发布清单写进
  [docs/build-notes.md](build-notes.md) §8。
- **顺手修复**：组件下载进度条此前把「填充条」当「轨道」用，进度不可见；
  现在与识别/加载进度条结构一致（含 `aria-valuenow`）。

## 行为变化（升级前请知悉）

| 变化 | 说明 |
|---|---|
| `outFormat` 白名单 | 支持 `md` / `txt` / `json`；非法值被拒且不落盘（旧文件里的脏值按 `md` 兜底） |
| 「保存」按钮 | `outFormat=json` 时导出结构化 JSON；历史文件没有 blocks 会提示重新识别，不导空文件 |
| CLI JSON | `blocks` 由「每页块列表」改为与 HTTP 同构的页信封 `[{"page": n, "blocks": [...]}]`；该字段是 v1.5.0 新增，旧脚本不受影响 |
| 组件下载 | 默认只告警不阻断；严格模式需显式设环境变量，且**拿不到期望哈希时仍不删包** |

## 已知限制

- 查找替换是**纯文本匹配**（大小写敏感、不引入正则），不支持「仅替换第 N 处」。
- JSON 导出取识别时的 blocks，**不随文本编辑重算**：编辑后导 `.md/.txt` 是编辑稿，
  JSON 仍是原始结构块；schema 未加 `edited` 字段（与后端落盘保持一致）。
- 查找导航会切到 Markdown 原文视图（渲染视图没有高亮）。
- 仍未做：docx/xlsx/可搜索 PDF/图表裁剪（E8/E9/E10/E15）、多文档合并导出（E16）、
  可配置输出目录（E19）、D4/D7/D15 发布流水线、cu129 组件档。
- 打包版仍无 CLI、EXE 仍无版本资源（沿用 v1.3.3）。

## 验证

- `tests/run_tests.py` → **756/756** 通过（新增 91 条）
- `tools/check.py` → 0 错误 0 告警；`node tools/frontend_sanitize_check.js` 22+20 全过
- headless Edge（真实 Chromium 内核）CDP → **18/18**（JSON 导出 blob 内容/缺 blocks 提示/
  查找替换/整篇只读/组件进度条）
- 真后端脚本 → **22/22**（build_document 六字段与阅读序、JSON 落盘结构/同名递增/无 tmp 残留、
  `outFormat` 校验、sha256 工具与 hashlib 对账）

## 升级提示

- 直接替换程序目录即可，`settings.json` / `models/` / `outputs/` 不受影响。
- 若你有脚本消费 CLI 的 JSON：`blocks` 字段形态在 v1.5.0 统一为页信封；
  其余字段与退出码不变。
