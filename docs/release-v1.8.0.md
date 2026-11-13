# v1.8.0 — Word / Excel 导出（零运行时依赖）

> 本版补上最常被要的两种交付格式：**Word（.docx）** 与 **Excel（.xlsx）**。
> 关键取舍：用 stdlib 手写 OOXML，**不引入 python-docx / openpyxl 等运行时依赖**
> （安装包体积与离线策略不变）。状态：**代码已就绪，未打包/未发布**。

## 亮点

- **Word 导出（.docx）**：标题按 `#`/层级映射 Heading 1–3、正文去掉 Markdown 行内标记、
  表格转成**真正的 Word 表格**（`colspan` → 合并列；每个表格后自动补段落避免表格粘连）、
  多页之间插分页符；文档属性带标题。
- **Excel 导出（.xlsx）**：每个表格一个工作表（表格1…N），`colspan/rowspan` →
  合并单元格（先合并、只写左上角值），纯数字自动识别为数值（`2.5` 是 float 不是文本），
  列宽按内容粗估。**只导出表格**——没有表格时给出明确错误，不产出空文件。
- **三种入口一致**：
  - GUI：设置页「输出格式」新增 Word/Excel；保存按钮走后端生成并下载
    （MIME/文件名正确）；自动落盘同样支持
  - CLI：`python cli.py --input scan.pdf --format docx|xlsx`
  - HTTP：`POST /api/export-office {fmt}` 返回 base64（只回字节、不写盘）
- **失败不连坐**：GUI 自动保存遇到「无结构化数据 / xlsx 没有表格」会记日志并
  **降级写 Markdown**，识别本身不失败（payload 用 `savedFormat` 报告实际格式）；
  CLI 不降级——拿不到 Office 文件就以退出码 2 明确失败。
- **输出可复现**：zip 内时间戳固定，同一输入生成的文件字节稳定。

## 行为变化（升级前请知悉）

| 变化 | 说明 |
|---|---|
| 输出格式 | `outFormat` 合法值扩展为 `md/txt/json/docx/xlsx`；旧 `settings.json` 无需迁移 |
| GUI 自动保存 | 选 Word/Excel 时写 `.docx/.xlsx`；缺少 blocks 或 xlsx 无表格时**降级为 `.md`**（日志有 WARN） |
| GUI 保存按钮 | 选 Word/Excel 时改为调用后端生成并下载；批量/历史结果会得到「缺少结构化结果」提示 |
| CLI | `--format docx|xlsx` 新增；无 blocks / 无表格 / 生成失败 → 退出码 **2**（不降级） |

## 已知限制

- **docx 的 rowspan 是保守近似**：不写 `vMerge`（避免半吊子合并触发 Word「修复」提示），
  合并区域文字放在起始格，其余格留空；需要精确跨行合并请用 xlsx（那边是真合并）。
- 图片块跳过（不嵌二进制）；标题超过 3 级压到 Heading 3；不做字体/单元格样式定制。
- xlsx 只导出表格；正文/标题不进 Excel（按报告 E10 的取舍）。
- 仍未做：图表区域裁剪（E15）、HTTP API 稳定化（E18）、历史缩略图（E20）、cu129 组件档、
  A8（PDF 直传 PIL，报告已明确不建议）。

## 验证

- `tests/run_tests.py` → **956/956** 通过（新增 81 条：写入器 37 + 后端 30 + 前端 14）
  - 其中「真解析库校验」类用 python-docx / openpyxl **仅作测试校验**（装在仓外
    `_work/validators`），标准套件无这些库时自动跳过、不失败
- `tools/check.py` → 0 错误 0 告警；`node tools/frontend_sanitize_check.js` 22+20 全过
- 真解析库校验结果：docx 样式含 Heading 1/2、表格行列与 colspan 展开正确；
  xlsx sheetnames/单元格值/`2.5` float/merged `A1:B1` 均可被 openpyxl 读回
- headless Edge CDP → **12/12**（Word/Excel 格式按钮、保存下载的 MIME 与文件名、
  失败进横幅、不产空文件）
- 真后端脚本 → **30/30**（`_save_output` 落盘与降级、端点 base64 不写盘、
  xlsx 无表格错误、CLI docx/xlsx 端到端可打开）

## 升级提示

- 直接替换程序目录即可；`settings.json` / `models/` / `outputs/` 不受影响。
- 想用 Office 导出：设置页把「输出格式」切到 Word 或 Excel 即可；旧结果（历史文件）
  没有结构化数据，导出会给出提示并要求重新识别。
