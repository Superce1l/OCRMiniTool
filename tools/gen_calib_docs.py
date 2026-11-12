# -*- coding: utf-8 -*-
"""tools/gen_calib_docs.py — 生成 D1–D6「密集校准文档」（token-budget 用）

为什么需要它
------------
`maxPageTokens` 默认 4096 是按"一页约 1000 字"拍的估值（实机长截图输出 973
token，见 docs/实机测试报告.md）。要知道这个默认值偏小还是偏大，必须拿**真实
密度**的文档去跑，看 `finish_reason` 会不会变成 `length`（截断）。色块与占位
文本测不出密度，所以这里用 Pillow + 系统字体画 6 份"像扫描件"的页面：

  D1 密集正文   1600×2200  单栏 45 行 × 约 40 字（≈1800 字）
  D2 密集表格   1600×2200  12 列 × 40 行 + 表头 + 合并单元格
  D3 公式符号   1600×2200  ∑ ∫ ∂ √、上下标、希腊字母、行内公式
  D4 双栏学术   1600×2200  标题横跨 + 摘要 + 左右双栏（中英，填满版面）
  D5 中英+代码  1600×2200  英文/中文段落 + 等宽代码块（行号、缩进、中文注释）
  D6 超长截图   600×3600   连续多段文字（h/w=6，模拟网页长截图，填满 3600px）

产物：`Dx.png` + `Dx.meta.json`（像素尺寸/行数/字数/生成参数）+ `index.json`。
默认输出到 `D:/Project/_work/calib/`（中间产物，不进仓库）。

字形安全（实测过的坑）
----------------------
微软雅黑缺少部分数学字形（∇ ∬ ∭ ∉ ⊂ ⇔ ⇒ ₀₁₂ ↔ …），Consolas 完全没有中文。
因此：
  • 数学符号/箭头/上下标数字 → 自动切换 Segoe UI Symbol（或 Cambria Math）
  • 代码块里的中文注释 → 自动切换微软雅黑
  • `--audit` 会逐字渲染比对 tofu 参考字形，把"会画成方框"的字符列出来

用法
----
    PYTHONPATH=.venv/Lib/site-packages python tools/gen_calib_docs.py
    python tools/gen_calib_docs.py --only D2,D6 --out D:/Project/_work/calib
    python tools/gen_calib_docs.py --list
    python tools/gen_calib_docs.py --audit      # 只做字形自检，不落盘

纯函数（visual_width / estimate_lines / text_stats / shuffled /
recommend_max_page_tokens / build_meta / DOC_SPECS）不依赖 Pillow，
tests/test_token_budget.py 可直接单测；绘制才 import PIL（延迟导入）。

注意：本脚本**只画文档，不跑推理**。第二阶段用这些图片跑引擎（串行、单实例）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = Path("D:/Project/_work/calib")
GENERATOR = "tools/gen_calib_docs.py"
DOC_VERSION = 1
SEED = 20261112

FONT_CJK = "C:/Windows/Fonts/msyh.ttc"        # 微软雅黑（正文/标题）
FONT_CJK_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
FONT_MONO = "C:/Windows/Fonts/consola.ttf"    # 代码块
FONT_SERIF = "C:/Windows/Fonts/times.ttf"     # 英文衬线（学术栏）
# 数学符号回退（按优先级探测；都缺时退回雅黑，--audit 会报警）
FONT_SYMBOL_CANDIDATES = (
    "C:/Windows/Fonts/seguisym.ttf",
    "C:/Windows/Fonts/cambria.ttc",
    "C:/Windows/Fonts/times.ttf",
)

# 单页 token 建议值的硬边界（与 docs/token-budget.md、tests 口径一致）
TOKEN_FLOOR = 1024
TOKEN_STEP = 1024
TOKEN_CAP = 8192

DOC_SPECS: list[dict] = [
    {
        "id": "D1", "kind": "dense_text", "title": "密集正文",
        "width": 1600, "height": 2200, "font_px": 36, "line_height": 45,
        "margin_x": 78, "margin_y": 70,
        "params": {"layout": "single-column", "font": "msyh.ttc",
                   "target_lines": 45, "target_chars_per_line": 40,
                   "footer": True},
        "note": "单栏 45 行 × 约 40 字（≈1800 字），模拟 A4 200dpi 扫描正文页",
    },
    {
        "id": "D2", "kind": "table", "title": "密集表格",
        "width": 1600, "height": 2200, "margin_x": 70, "margin_y": 70,
        "params": {"rows": 40, "cols": 12, "font_px": 20, "header_font_px": 21,
                   "title_row": True, "header_row": True,
                   "merges": 3, "footer": True},
        "note": "12 列 × 40 行物料盘点表：表头 + 数字 + 合并单元格（标题行/小计/合计）",
    },
    {
        "id": "D3", "kind": "math", "title": "公式符号页",
        "width": 1600, "height": 2200, "margin_x": 90, "margin_y": 70,
        "params": {"layout": "single-column", "font_px": 27, "formula_font_px": 29,
                   "line_height": 46, "inline_sup_sub": True, "greek": True},
        "note": "∑ ∫ ∂ √ 与上下标、希腊字母、行内/独立公式（模拟教材扫描页）",
    },
    {
        "id": "D4", "kind": "two_column", "title": "双栏学术排版",
        "width": 1600, "height": 2200, "margin_x": 72, "margin_y": 64,
        "params": {"layout": "two-column", "title_px": 40, "body_px": 22,
                   "abstract_px": 24, "gutter_px": 44, "columns": 2,
                   "en_font": "times.ttf", "fill_column": True},
        "note": "标题横跨 + 摘要 + 左右双栏正文（左中文/右英文，含分栏线，填满版面）",
    },
    {
        "id": "D5", "kind": "code_mix", "title": "中英混排+代码块",
        "width": 1600, "height": 2200, "margin_x": 74, "margin_y": 64,
        "params": {"layout": "single-column", "body_px": 26, "mono_px": 21,
                   "code_line_numbers": True, "code_blocks": 2, "indent": 4},
        "note": "英文段落 + 中文段落 + 等宽代码块（行号、缩进、中文注释）",
    },
    {
        "id": "D6", "kind": "long_shot", "title": "超长截图",
        "width": 600, "height": 3600, "margin_x": 24, "margin_y": 24,
        "params": {"layout": "single-column", "font_px": 18, "line_height": 26,
                   "hw_ratio": 6.0, "sections": 6, "bullets": True,
                   "fill_page": True},
        "note": "600×3600（h/w=6）连续多段文字 + 小标题/列表，填满整张长图",
    },
]

SPEC_BY_ID = {s["id"]: s for s in DOC_SPECS}

# ── 语料（确定性；D1/D4/D6 用带 seed 的洗牌避免相邻重复）────────
CORPUS_ZH = [
    "文档版面理解需要在保持阅读顺序的前提下，把页面上的文字、表格与图注映射成结构化表示。",
    "扫描件的字体渲染、行距与字符间距往往与原稿存在细微差异，这会直接影响识别结果的断行位置。",
    "在密集正文页中，单行字符数通常介于三十八到四十二之间，行与行之间共享统一的基线网格。",
    "模型对中文标点的处理容易被误判为噪声，尤其是间隔号、引号与括号的成对关系。",
    "为了保证跨页段落不被切断，合并解析会把多页放进同一次请求，并在输出里用分页标记分隔。",
    "表格区域的关键在于列宽与行高的比例，一旦比例失衡，单元格内的数字就会与边框粘连。",
    "公式页的难点是上下标的基线偏移，若偏移量被压缩，指数与下标会退化成普通字符。",
    "双栏排版的阅读顺序应当先左后右，但标题与摘要必须横跨两栏，不能被误切成两段。",
    "代码块使用等宽字体与固定缩进，行号的存在会让字符间距比正文更紧凑，极易触发换行误判。",
    "长截图的宽高比往往超过四比一，动态切片在这种版面上会退化成很窄的竖条，反而更慢。",
    "图像预处理阶段的缩放会改变笔画粗细，过度的锐化会制造出并不存在的衬线。",
    "对于带底纹的表格，文字与背景的对比度下降，识别时容易把相邻两列合并成一个单元格。",
    "跨页表格的表头通常会在每页重复出现，合并时需要识别重复行并只保留一份。",
    "页眉页脚与页码属于版面噪声，但在部分版式中它们承载了章节信息，不能一概丢弃。",
    "参考文献列表的悬挂缩进会让首行与续行不对齐，断行位置因此比正文更难预测。",
    "中文与英文混排时，词间空格的缺失会让分词边界变得模糊，进而影响断句。",
    "数学符号的字体回退经常发生，求和号与积分号的上下限可能被渲染成独立的小字号文本。",
    "如果一页的正文密度接近上限，模型输出会触及单页生成上限并被标记为截断。",
    "校准的目的不是追求更大的上限，而是让绝大多数页在合理的时间内完整输出。",
    "在纯处理器上，解码速度受内存带宽限制，输出越长耗时越接近线性增长。",
    "页面倾斜角度超过两度时，行基线会出现整体偏移，逐行切分的假定随之失效。",
    "印章与手写批注属于非文本内容，它们会遮挡正文，导致局部字符缺失或粘连。",
    "单元格内的换行符会让一个逻辑行变成两行，行高估算因此出现系统性偏差。",
    "公式编号通常右对齐，编号与公式之间以制表位分隔，识别时容易被当作两个独立块。",
    "摘要段落常使用较小的字号与更宽的字距，与正文的排版参数并不一致。",
    "图注一般位于图下方并居中，字号小于正文，与正文之间的垂直间距也更小。",
    "在双栏中插入跨栏图表时，阅读顺序会先沿图表上方两栏，再回到图表下方。",
    "代码中的空行与注释行不含语义信息，但它们占据了固定的垂直空间。",
    "当一页同时出现横排与竖排文本时，方向检测的误差会显著放大。",
    "表格中的单位行往往合并整行，若按列切分会把单位与列名混在一起。",
    "页码有时位于页脚中央，有时位于外侧页边距，位置并不固定。",
    "低对比度的灰色正文在二值化后可能断裂成碎片，笔画细节随之丢失。",
    "合并单元格的文本通常左对齐，而数值列右对齐，这一差异可以作为切分线索。",
    "同一份文档内的页边距应保持一致，若出现突变，多半是插入了不同来源的页面。",
    "目录页包含大量点线引导符，这些符号在识别结果里往往表现为连续的句点。",
    "中英文混排时标点宽度不同，导致行尾对齐出现细微的锯齿。",
    "列表项之间的间距大于行内间距，这一特征有助于恢复列表结构。",
    "脚注位于页面底部并以分隔线标记，字号明显小于正文。",
    "文档中的超链接带有下划线，下划线与文字粘连会影响字符切分。",
    "扫描分辨率不足时，汉字的横画会断裂，竖画则容易粘连。",
    "双面扫描的奇数页与偶数页页边距镜像对称，装订线一侧留白更宽。",
    "表格中的负号与括号表示负数，若符号与数字粘连会被识别成正数。",
    "公式中的括号有大小分级，自动缩放失败时会出现明显的基线跳动。",
    "页眉中的章节标题与正文标题重复，合并解析时需要去重而不是简单拼接。",
    "如果正文使用了首行缩进，段落的起始位置会随缩进量整体右移。",
    "表格的边框线粗细不一，外框通常更粗，内部网格线更细。",
    "斜体英文在低分辨率下会与相邻字符粘连，词边界因此变得模糊。",
    "大写字母与小写字母的高度差异是判断字号的重要依据。",
    "文档末尾的空白页不含内容，但仍会被计入页数，需要在合并时跳过。",
    "校准文档的尺寸应当覆盖常见的扫描分辨率，避免只在单一尺度上验证。",
    "识别结果中的版面标记并非用户可见内容，统计 token 时需要把它们算进去。",
    "装订线附近的文字最容易变形，因为纸张在装订时被挤压出弧度。",
    "彩色印章在灰度化之后与正文的亮度接近，局部对比度骤降。",
    "表格跨页时通常会在下一页重复表头，有时还会追加「续表」字样。",
    "公式中的分数在行内排版时会压缩高度，分子分母的基线随之改变。",
    "英文单词的连字符断行会让同一行出现两个不同字号的片段。",
    "页边批注以竖排方式书写，方向与正文相差九十度。",
    "纸张泛黄会使整体背景偏暖，二值化阈值因此需要自适应。",
    "复印件上的墨粉颗粒会造成孤立噪点，噪点与标点的形状相近。",
    "同一段落内字号突然变化，往往意味着混入了不同来源的文本。",
    "扫描时的轻微抖动会让竖线变成波浪线，表格边框因此不直。",
    "行距过密时上下两行的笔画可能粘连，字符切分随之失败。",
    "目录中的页码右对齐并使用前导点，点线在识别时容易被当作句号。",
    "图表标题位于表格上方时，它属于表格块而不是独立的正文段。",
    "手写签名与印刷体混排，签名部分通常无法被正确识别。",
    "页脚中的版权声明字号很小，但字符数量并不少。",
    "公式中的矩阵使用方括号包裹，括号高度随行数增加而自动放大。",
    "双栏排版中的通栏图表会把页面切成上下两个阅读区域。",
    "文档中的水印位于背景层，灰度较浅，容易与噪点混淆。",
    "同一页内同时存在横线与竖线时，方向判定需要按区域进行。",
    "扫描件的边缘常有黑边，裁切不当会把黑边识别成粗线。",
    "段落之间的空行有时被压缩为零，段落结构因此只能靠缩进推断。",
    "参考文献的编号使用方括号，方括号与数字之间的间距很窄。",
    "表格数值使用千分位逗号，逗号与数字粘连时会被识别成小数点。",
]
CORPUS_EN = [
    "Layout analysis aims to recover the reading order of a scanned page while keeping the "
    "bounding boxes of text, tables, and captions consistent with the original document.",
    "Dense single-column pages typically carry fifty to sixty characters per line in Latin "
    "scripts and far fewer when the column is narrow, which changes the wrapping behaviour.",
    "Two-column academic papers place the title and the abstract across the full measure, "
    "then switch to a narrower measure for the body, with a visible gutter between columns.",
    "Monospaced code blocks preserve indentation, so a single mixed indentation style is a "
    "strong signal that the page was typeset as source code rather than as prose.",
    "Long screenshots are common in documentation portals; their aspect ratio often exceeds "
    "four to one, which makes dynamic tiling counter-productive for both speed and accuracy.",
    "A calibration corpus should cover the shape of real inputs rather than synthetic blobs, "
    "otherwise the measured token distribution says nothing about production workloads.",
    "Tables with merged cells break the regular grid, and the renderer must still keep every "
    "cell readable after rasterisation at two hundred dots per inch.",
    "Mathematical notation mixes baselines, superscripts and subscripts; when the vertical "
    "offsets collapse, exponents are read as ordinary characters and the formula is lost.",
    "The measured token distribution is a property of the language model, not of the page "
    "geometry, so two documents with identical layouts may still differ substantially.",
    "Calibration should report percentiles rather than a single average, because a handful "
    "of dense pages can dominate the worst-case latency of an interactive tool.",
    "When the decoder hits its limit, the stop reason becomes length; treating that as a "
    "successful recognition would silently drop the tail of the page.",
    "Empty margins are not free: they increase the raster size, the vision tower cost, and "
    "the time spent on tiles that contain no text at all.",
    "A well-formed table keeps its header on every page; a naive merge would duplicate the "
    "header rows and inflate the output length by a small but measurable amount.",
    "Code comments in Chinese force the renderer to mix a proportional CJK font with a "
    "monospaced Latin font, which is exactly the kind of stress test a corpus needs.",
    "Footnotes are set in a smaller size and separated by a short rule; their presence "
    "changes the vertical rhythm of the lower part of the page.",
    "Hyphenation is a language-specific decision, and a page that breaks words at line ends "
    "looks different from one that does not, although both are valid typesetting.",
    "The worst case for latency is not the average page but the densest page, which is why "
    "the calibration set deliberately includes a fully populated twelve-column table.",
    "Any recommendation about output limits should be traceable to measurements: which "
    "document, which stop reason, and how many output tokens it actually produced.",
    "A page that mixes Chinese characters with Latin abbreviations forces the tokenizer to "
    "switch vocabularies constantly, which changes the token-per-character ratio.",
    "Margins are part of the page geometry, but they are not part of the content; a crop "
    "that removes them reduces the raster cost without changing the recognised text.",
    "When two columns share a single heading, the reading order must return to the left "
    "column before advancing, even if the right column is visually higher.",
    "Repeated headers across pages are a normal artefact of book binding, and de-duplicating "
    "them is a layout decision rather than an OCR decision.",
    "The choice between tiling and letterboxing depends on the aspect ratio of the page, not "
    "on the language of its content.",
    "Small caps and superscripts are easy to confuse with ordinary capitals when the image "
    "resolution is close to the stroke width.",
    "A table whose first column contains merged cells cannot be parsed by counting vertical "
    "rules alone; the merge pattern has to be recovered from the text as well.",
    "Long outputs cost time linearly on a CPU, so the marginal value of a larger limit "
    "should be weighed against the worst-case latency.",
    "Calibration documents are deliberately synthetic, but their density should mirror the "
    "densest real pages the tool is expected to handle.",
    "If the decoder stops because of a length limit, the tail of the page is missing and the "
    "result should be flagged instead of silently accepted.",
    "Footnotes, captions and page numbers are all small text, yet they carry distinct layout "
    "roles that a downstream reader may need to reconstruct.",
    "The reading order of a two-column page is not simply left-to-right; it is column by "
    "column within the body, but full-width for the title and abstract.",
    "Blank space is not neutral: it changes the aspect ratio, the tiling policy, and the "
    "amount of visual context the model receives in one request.",
    "A dense table with twelve columns leaves little room for text, so the renderer must "
    "choose a font size that keeps every cell readable after rasterisation.",
    "Measurements taken on one machine do not transfer directly to another, but the relative "
    "ordering of documents by output length is usually stable.",
    "Hyphenated words split across lines are a classic source of spurious tokens when the "
    "recogniser treats the hyphen as punctuation.",
    "When a page contains both prose and code, the vertical rhythm changes abruptly, and the "
    "column-detection step may misread the code block as a figure.",
    "The most useful summary of a calibration run is a small table of document, output "
    "tokens, stop reason and elapsed time, repeated for each limit under test.",
    "A limit that is generous enough for the worst page is also generous enough for the "
    "average one, but it makes the average case slower; percentiles resolve the tension.",
]
# (kind, text) —— 公式页逐行；^{..}/_{..} 由渲染器画成真上标/下标
CORPUS_MATH = [
    ("h1", "第三章　多元函数微分学与积分变换"),
    ("h2", "3.1　偏导数、全微分与梯度"),
    ("body", "设函数 f(x, y) 在点 P₀(x₀, y₀) 的某邻域内有定义，若极限存在，则称该极限为 f 对 x 的偏导数。"),
    ("formula", "∂f/∂x = lim_{Δx→0} [f(x₀+Δx, y₀) − f(x₀, y₀)] / Δx"),
    ("formula", "∂f/∂y = 2x + 3y^{2}，∂²f/∂x∂y = 6y"),
    ("body", "梯度向量 ∇f = (∂f/∂x, ∂f/∂y) 指向函数增长最快的方向，其模长等于方向导数的最大值。"),
    ("formula", "∇f(P₀) = (2, −1)，|∇f| = √(2² + (−1)²) = √5"),
    ("h2", "3.2　重积分与累次积分"),
    ("body", "若 f 在闭区域 D 上连续，则二重积分可化为累次积分，积分次序的交换需要区域满足正则性条件。"),
    ("formula", "∬_{D} f(x, y) dxdy = ∫_{a}^{b} dx ∫_{φ₁(x)}^{φ₂(x)} f(x, y) dy"),
    ("formula", "∫_{0}^{1} ∫_{0}^{1−x} (x + y) dy dx = 1/3"),
    ("body", "三重积分在球坐标下的体积元为 r² sin θ dr dθ dφ，其中 θ 为极角、φ 为方位角。"),
    ("formula", "∭_{Ω} f dV = ∫_{0}^{2π} dφ ∫_{0}^{π} sinθ dθ ∫_{0}^{R} f(r) r² dr"),
    ("h2", "3.3　级数与收敛性"),
    ("formula", "∑_{n=1}^{∞} 1/n^{2} = π²/6，∑_{n=0}^{∞} x^{n}/n! = e^{x}"),
    ("body", "比值判别法：若 lim_{n→∞} |a_{n+1}/a_{n}| = ρ < 1，则级数绝对收敛；ρ > 1 时发散。"),
    ("formula", "lim_{n→∞} (1 + 1/n)^{n} = e ≈ 2.718281828"),
    ("body", "幂级数的收敛半径 R 由根值判别法确定，端点处的敛散性需要单独讨论。"),
    ("formula", "R = 1 / limsup_{n→∞} ⁿ√|a_{n}|"),
    ("h2", "3.4　常微分方程"),
    ("body", "一阶线性方程 y′ + P(x)y = Q(x) 的通解可用积分因子 μ(x) = exp(∫P dx) 求得。"),
    ("formula", "y = e^{−∫P dx} (∫ Q e^{∫P dx} dx + C)"),
    ("formula", "y″ − 3y′ + 2y = 0 ⇒ λ² − 3λ + 2 = 0 ⇒ y = C₁e^{x} + C₂e^{2x}"),
    ("body", "二阶常系数非齐次方程的特解形式取决于右端项的类型，需要与齐次解比较后确定。"),
    ("h2", "3.5　常用符号与希腊字母"),
    ("body", "α β γ δ ε ζ η θ ι κ λ μ ν ξ ο π ρ σ τ υ φ χ ψ ω"),
    ("body", "Γ Δ Θ Λ Ξ Π Σ Υ Φ Ψ Ω　∑ ∏ ∫ ∬ ∭ ∂ ∇ √ ∞ ≤ ≥ ≠ ≈ ≡ ⇒ ⇔ ∈ ∉ ⊂ ∪ ∩ ± × ÷ · °"),
    ("formula", "矩阵 A ∈ R^{m×n}，det(A) ≠ 0 ⇔ A 可逆 ⇔ 秩(A) = n"),
    ("formula", "A^{T}A = I ⇒ A 为正交矩阵；λ_{i} 为特征值，∑ λ_{i} = tr(A)"),
    ("body", "不等式：√(a² + b²) ≤ |a| + |b|，且 (a + b)² ≤ 2(a² + b²)。"),
    ("formula", "|∫_{a}^{b} f(x)g(x) dx| ≤ (∫_{a}^{b} f² dx)^{1/2} (∫_{a}^{b} g² dx)^{1/2}"),
    ("body", "概率密度 p(x) 满足 ∫ p(x) dx = 1，期望 E[X] = ∫ x p(x) dx，方差 Var(X) = E[X²] − (E[X])²。"),
    ("formula", "P(A|B) = P(B|A) P(A) / P(B)，H(X) = −∑ p_{i} log₂ p_{i}"),
    ("h2", "3.6　近似与误差"),
    ("body", "泰勒展开在 x₀ 处的前 n 项给出局部近似，其余项 R_n 的阶决定了近似的可信区间。"),
    ("formula", "f(x) = f(x₀) + f′(x₀)(x−x₀) + f″(ξ)(x−x₀)²/2"),
    ("formula", "√(1 + x) ≈ 1 + x/2 − x²/8 + O(x³)，|x| < 1"),
    ("body", "数值积分的梯形公式误差为 −(b−a)³ f″(ξ)/12，辛普森公式的误差阶提高到五次方。"),
    ("formula", "∫_{a}^{b} f dx ≈ (b−a)/6 · [f(a) + 4f((a+b)/2) + f(b)]"),
    ("body", "迭代格式 x_{k+1} = x_{k} − f(x_{k})/f′(x_{k}) 在单根附近二阶收敛。"),
    ("formula", "|x_{k+1} − x*| ≤ C |x_{k} − x*|²，C = max|f″| / (2 min|f′|)"),
    ("h2", "3.7　傅里叶变换与卷积"),
    ("body", "连续傅里叶变换把时域卷积化为频域乘积，这一性质是快速算法的理论依据。"),
    ("formula", "F(ω) = ∫_{−∞}^{∞} f(t) e^{−iωt} dt，f(t) = (1/2π) ∫ F(ω) e^{iωt} dω"),
    ("formula", "f * g (t) = ∫ f(τ) g(t−τ) dτ ⇔ F(ω)G(ω)"),
    ("formula", "δ(t) = (1/2π) ∫ e^{iωt} dω，∫ δ(t) dt = 1"),
    ("h2", "3.8　线性代数补充"),
    ("body", "实对称矩阵的特征值均为实数，且不同特征值对应的特征向量互相正交。"),
    ("formula", "A = QΛQ^{T}，Λ = diag(λ₁, …, λ_{n})，Q^{T}Q = I"),
    ("formula", "‖x‖₂ = (∑ x_{i}²)^{1/2}，‖Ax‖ ≤ ‖A‖ ‖x‖"),
    ("body", "奇异值分解把任意矩阵写成两个正交矩阵与一个对角矩阵的乘积。"),
    ("formula", "A = UΣV^{T}，σ₁ ≥ σ₂ ≥ … ≥ 0，rank(A) = 非零奇异值个数"),
    ("body", "注：以上为校准文档内容，用于测量密集符号页的 token 密度，不追求数学严谨性。"),
]
CODE_SAMPLE_1 = '''def plan_groups(pages, merge_limit):
    """把页切成组；组间不共享上下文。"""
    if merge_limit <= 0:
        return [pages]
    groups = []
    for start in range(0, len(pages), merge_limit):
        groups.append(pages[start:start + merge_limit])
    return groups


def effective_tokens(page_tokens, n_pages, explicit=None):
    caps = []
    if page_tokens:
        caps.append(page_tokens * max(1, n_pages))
    if explicit:
        caps.append(explicit)
    return max(1, min(min(caps), 131072))
'''
CODE_SAMPLE_2 = '''class Budget:
    """按页累计输出 token，超限时抛错。"""

    def __init__(self, limit=4096):
        self.limit = limit
        self.used = 0

    def take(self, n):
        if self.used + n > self.limit:
            raise ValueError("token budget exceeded")
        self.used += n
        return self.used
'''


# ══════════════════════════════════════════════════════════════════
# 纯函数（不依赖 Pillow：tests/test_token_budget.py 直接单测）
# ══════════════════════════════════════════════════════════════════
def _is_wide(code: int) -> bool:
    """宽字符（CJK/全角标点）：按 1 个"字宽"计。数学符号不算宽字符。"""
    return (
        0x1100 <= code <= 0x115F
        or 0x2E80 <= code <= 0xA4CF
        or 0xAC00 <= code <= 0xD7A3
        or 0xF900 <= code <= 0xFAFF
        or 0xFE30 <= code <= 0xFE4F
        or 0xFF00 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
        or 0x1F300 <= code <= 0x1FAFF
    )


def _is_symbol_char(code: int) -> bool:
    """需要数学/符号字体回退的码位（微软雅黑在这些区段缺字形，实测）。"""
    ranges = (
        (0x2070, 0x209F),   # 上下标数字
        (0x2100, 0x214F),   # letterlike
        (0x2190, 0x21FF),   # 箭头
        (0x2200, 0x22FF),   # 数学运算符
        (0x2300, 0x23FF),   # 杂项技术
        (0x25A0, 0x25FF),   # 几何图形
        (0x27C0, 0x27EF),   # 数学运算扩展
        (0x2980, 0x29FF),   # 杂项数学符号
        (0x2A00, 0x2AFF),   # 补充数学运算符
    )
    return any(lo <= code <= hi for lo, hi in ranges)


def visual_width(text: str) -> float:
    """估算字符串的显示宽度（以"一个汉字的宽度"为单位，ASCII 约 0.55）。"""
    total = 0.0
    for ch in str(text):
        if ch in "\n\r\t":
            continue
        total += 1.0 if _is_wide(ord(ch)) else 0.55
    return total


def estimate_lines(text: str, chars_per_line: int) -> int:
    """按字宽折算行数：每个换行段至少 1 行，向上取整。"""
    if chars_per_line <= 0:
        raise ValueError("chars_per_line must be positive")
    lines = 0
    for para in str(text).split("\n"):
        lines += max(1, math.ceil(visual_width(para) / chars_per_line))
    return lines


def text_stats(texts) -> dict:
    """统计"写进图里的文字"：总字符数（不含空白）、中文/拉丁/数字/符号分项。"""
    joined = "".join(str(t) for t in texts)
    cjk = latin = digit = symbol = 0
    for ch in joined:
        if ch.isspace():
            continue
        code = ord(ch)
        if _is_wide(code):
            cjk += 1
        elif ch.isdigit():
            digit += 1
        elif ch.isascii() and ch.isalpha():
            latin += 1
        else:
            symbol += 1
    return {
        "chars_total": cjk + latin + digit + symbol,
        "chars_cjk": cjk,
        "chars_latin": latin,
        "chars_digit": digit,
        "chars_symbol": symbol,
    }


def shuffled(items, seed: int = SEED) -> list:
    """确定性洗牌（同 seed 同顺序；用于避免相邻段落重复）。"""
    out = list(items)
    random.Random(int(seed)).shuffle(out)
    return out


def recommend_max_page_tokens(p95_tokens: float, *, factor: float = 1.3,
                              step: int = TOKEN_STEP, floor: int = TOKEN_FLOOR,
                              cap: int = TOKEN_CAP) -> int:
    """建议默认值公式：clamp(ceil(ceil(p95×1.3)/step)×step, floor, cap)。

    • p95（95 分位）覆盖长尾，而不是拿最大值把常态页拖慢
    • ×1.3 留余量（版式抖动、模型输出标记 token）
    • 向上取整到 1024 的倍数：UI 步进/分档整齐
    • 硬边界 [1024, 8192]：下限防普通页频繁截断，上限对"最坏单页时长"负责
      （CPU 约 9 tok/s 时 8192 ≈ 15 分钟/页，见 docs/token-budget.md）
    """
    try:
        value = float(p95_tokens or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        return int(floor)
    want = math.ceil(value * float(factor))
    steps = math.ceil(want / int(step))
    return int(max(floor, min(cap, steps * int(step))))


def build_meta(spec: dict, *, lines: int, char_stats: dict, params: dict | None = None,
               image: tuple[int, int] | None = None) -> dict:
    """组装单份文档的 meta（纯函数：不读文件、不 import Pillow）。"""
    width, height = image if image else (int(spec["width"]), int(spec["height"]))
    stats = dict(char_stats or {})
    return {
        "schema": "ocrmini/calib-doc@1",
        "id": spec["id"],
        "kind": spec["kind"],
        "title": spec["title"],
        "note": spec.get("note", ""),
        "file": f"{spec['id']}.png",
        "image": {
            "width": int(width),
            "height": int(height),
            "hw_ratio": round(float(height) / float(width), 3) if width else None,
        },
        "content": {
            "lines": int(lines),
            "chars_total": int(stats.get("chars_total", 0)),
            "chars_cjk": int(stats.get("chars_cjk", 0)),
            "chars_latin": int(stats.get("chars_latin", 0)),
            "chars_digit": int(stats.get("chars_digit", 0)),
            "chars_symbol": int(stats.get("chars_symbol", 0)),
            "chars_per_line": round(
                float(stats.get("chars_total", 0)) / max(1, int(lines)), 2),
        },
        "params": dict(params if params is not None else spec.get("params", {})),
        "generator": {"script": GENERATOR, "version": DOC_VERSION},
    }


# ══════════════════════════════════════════════════════════════════
# Pillow 绘制（延迟 import）
# ══════════════════════════════════════════════════════════════════
def _require_pil():
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401
    except Exception as e:  # pragma: no cover - 环境问题
        raise RuntimeError(
            "需要 Pillow：本机用 `PYTHONPATH=.venv/Lib/site-packages python "
            "tools/gen_calib_docs.py`（或 pip install pillow）") from e
    return True


_FONT_CACHE: dict = {}
_SYMBOL_PATH_CACHE: list = []


def _font(path: str, size: int):
    from PIL import ImageFont
    key = (path, int(size))
    if key not in _FONT_CACHE:
        try:
            _FONT_CACHE[key] = ImageFont.truetype(path, int(size), index=0)
        except Exception:
            _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]


def symbol_font_path() -> str:
    """首个存在的符号字体（Segoe UI Symbol / Cambria Math / Times）。"""
    if not _SYMBOL_PATH_CACHE:
        for cand in FONT_SYMBOL_CANDIDATES:
            if Path(cand).is_file():
                _SYMBOL_PATH_CACHE.append(cand)
                break
        else:
            _SYMBOL_PATH_CACHE.append(FONT_CJK)      # 最后兜底
    return _SYMBOL_PATH_CACHE[0]


def _font_for_char(ch: str, role: str, size: int):
    """按字符选字体：数学符号 → 符号字体；代码里的中文 → 雅黑；其余按角色。"""
    code = ord(ch)
    if _is_symbol_char(code):
        return _font(symbol_font_path(), size)
    if role == "mono":
        return _font(FONT_CJK if _is_wide(code) else FONT_MONO, size)
    if role == "serif":
        return _font(FONT_CJK if _is_wide(code) else FONT_SERIF, size)
    return _font(FONT_CJK, size)


def _line_height(px: int, ratio: float = 1.45) -> int:
    return max(int(px * ratio), int(px) + 4)


def _draw_runs(draw, x: int, y: int, text: str, role: str, size: int,
               fill=(0, 0, 0)) -> float:
    """逐字符按字体分组绘制（处理字形回退），返回结束 x。"""
    cur_font = None
    buf = ""
    for ch in str(text):
        font = _font_for_char(ch, role, size)
        if cur_font is None:
            cur_font = font
        if font is not cur_font:
            draw.text((x, y), buf, font=cur_font, fill=fill)
            x += cur_font.getlength(buf)
            buf = ""
            cur_font = font
        buf += ch
    if buf:
        draw.text((x, y), buf, font=cur_font, fill=fill)
        x += cur_font.getlength(buf)
    return x


def _measure(text: str, role: str, size: int) -> float:
    total = 0.0
    cache: dict = {}
    for ch in str(text):
        font = _font_for_char(ch, role, size)
        key = id(font)
        if key not in cache:
            cache[key] = font
        total += font.getlength(ch)
    return total


def _units(text: str) -> list[str]:
    """切成换行单元：CJK 逐字，ASCII 连绵词，空白单独成单元。"""
    out: list[str] = []
    buf = ""
    for ch in str(text):
        if _is_wide(ord(ch)) or ch in " \t":
            if buf:
                out.append(buf)
                buf = ""
            out.append(ch)
        else:
            buf += ch
    if buf:
        out.append(buf)
    return out


def _wrap(text: str, role: str, size: int, max_width: int) -> list[str]:
    """按像素宽度贪心换行（CJK 逐字、ASCII 按词）。"""
    lines: list[str] = []
    cur = ""
    for unit in _units(str(text)):
        cand = cur + unit
        if cur and _measure(cand, role, size) > max_width:
            lines.append(cur.rstrip())
            cur = unit.lstrip()
        else:
            cur = cand
    if cur.strip() or not lines:
        lines.append(cur.rstrip())
    return lines


def _draw_rich(draw, x: int, y: int, text: str, size: int, role: str = "text",
               fill=(0, 0, 0)) -> float:
    """画一行富文本：支持 ^{...} 上标与 _{...} 下标（公式页用）。"""
    small = max(10, int(size * 0.68))
    cur = 0.0
    buf = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "^_" and i + 1 < len(text):
            if buf:
                cur = _draw_runs(draw, x + cur, y, buf, role, size, fill) - x
                buf = ""
            j = i + 1
            if text[j] == "{":
                k = text.find("}", j)
                k = k if k > 0 else len(text)
                body = text[j + 1:k]
                i = k + 1
            else:
                body = text[j]
                i = j + 1
            dy = int(size * -0.34) if ch == "^" else int(size * 0.24)
            end = _draw_runs(draw, x + cur, y + dy, body, role, small, fill)
            cur += end - (x + cur)
            continue
        buf += ch
        i += 1
    if buf:
        cur = _draw_runs(draw, x + cur, y, buf, role, size, fill) - x
    return x + cur


def _draw_paragraph(draw, x: int, y: int, text: str, role: str, size: int,
                    max_width: int, line_h: int, *, fill=(0, 0, 0),
                    bottom: int | None = None) -> tuple[int, int]:
    """画一段自动换行的文字，返回 (已画行数, 结束 y)。"""
    drawn = 0
    for line in _wrap(text, role, size, max_width):
        if bottom is not None and y + line_h > bottom:
            break
        _draw_runs(draw, x, y, line, role, size, fill)
        y += line_h
        drawn += 1
    return drawn, y


# ── 各类版式 ───────────────────────────────────────────────────
def render_dense_text(draw, img, spec) -> dict:
    """密集正文：把语料拼成一条连续文本流再按像素宽度换行。

    连续流（而不是一段段各自换行）是有意的：真实书页没有"每段最后一行留白"，
    逐段换行会人为降低字符密度，让 token 数偏低。
    """
    px = spec["font_px"]
    lh = spec["line_height"]
    mx, my = spec["margin_x"], spec["margin_y"]
    max_w = img.width - 2 * mx
    bottom = img.height - my
    target = spec["params"]["target_lines"]
    order = shuffled(CORPUS_ZH, SEED)
    stream = ""
    i = 0
    need = target * int(spec["params"]["target_chars_per_line"])
    while visual_width(stream) < need and i < len(order) * 4:
        stream += order[i % len(order)]
        i += 1
    lines = _wrap(stream, "text", px, max_w)
    while len(lines) < target and i < len(order) * 8:
        stream += order[i % len(order)]
        i += 1
        lines = _wrap(stream, "text", px, max_w)
    lines = lines[:target]
    texts: list[str] = []
    y = my
    body_lines = 0
    for line in lines:
        if y + lh > bottom:
            break
        _draw_runs(draw, mx, y, line, "text", px)
        texts.append(line)
        y += lh
        body_lines += 1
    if spec["params"].get("footer"):
        small = max(14, px // 2)
        footer = "第 1 页 / 共 1 页"
        _draw_runs(draw, img.width // 2 - 60, img.height - my // 2, footer,
                   "text", small, (90, 90, 90))
        texts.append(footer)
    return {"lines": body_lines, "texts": texts}


def render_table(draw, img, spec) -> dict:
    """12 列 × 40 行物料盘点表：标题行/表头/数据行 + 3 处合并单元格。

    合并单元格用「起始格 → (跨列数, 文本, 对齐)」描述，绘制时跳过被吞掉的格子：
      • 第 1 个数据行最后两列合并（备注列留白）
      • 第 20 个数据行前两列合并（「小计」）
      • 最后一整行横跨 12 列（合计行，浅底）
    """
    px = spec["params"]["font_px"]
    hpx = spec["params"]["header_font_px"]
    mx, my = spec["margin_x"], spec["margin_y"]
    rows, cols = spec["params"]["rows"], spec["params"]["cols"]
    title_h, header_h, row_h = 66, 56, 46
    table_w = img.width - 2 * mx
    fracs = [0.052, 0.085, 0.105, 0.092, 0.062, 0.055,
             0.080, 0.092, 0.058, 0.068, 0.082, 0.169]
    widths = [int(round(table_w * f)) for f in fracs]
    widths[-1] += table_w - sum(widths)          # 吸收取整误差
    xs = [mx]
    for w in widths:
        xs.append(xs[-1] + w)
    f_head = _font(FONT_CJK_BOLD, hpx)
    f_title = _font(FONT_CJK_BOLD, 26)
    ink = (40, 40, 40)
    head_fill = (238, 238, 238)
    texts: list[str] = []
    lines = 0

    # 标题行（横跨 12 列 = 合并单元格）
    y = my
    title = "2026 年第三季度物料盘点表（华东仓）"
    draw.rectangle([mx, y, mx + table_w, y + title_h], fill=head_fill,
                   outline=ink, width=2)
    draw.text((mx + table_w // 2, y + title_h // 2), title, font=f_title,
              fill=ink, anchor="mm")
    texts.append(title)
    lines += 1
    y += title_h

    header = ["序号", "物料编码", "名称", "规格", "数量", "单位",
              "单价", "金额", "税率", "仓库", "批次", "备注"]
    for c, label in enumerate(header):
        draw.rectangle([xs[c], y, xs[c + 1], y + header_h], fill=head_fill,
                       outline=ink, width=1)
        draw.text((xs[c] + 6, y + header_h // 2), label, font=f_head,
                  fill=ink, anchor="lm")
        texts.append(label)
    lines += 1
    y += header_h

    names = ["六角螺栓", "平垫圈", "弹簧垫圈", "轴承", "密封圈", "同步带",
             "齿轮", "联轴器", "导轨", "气缸", "电磁阀", "滤芯"]
    specs_col = ["M8×20", "M10×30", "Φ20×40", "6204-2RS", "TC-25×40", "HTD-5M"]
    whs = ["A-01", "A-02", "B-03", "B-07", "C-02"]
    merges: dict[tuple[int, int], tuple[int, str, str]] = {
        (1, 11): (2, "—", "c"),
        (20, 1): (2, "小计", "l"),
        (rows, 1): (cols, "合计：48,600.00 元（含税）　制表：王工　"
                          "复核：李工　日期：2026-11-12", "c"),
    }

    def _cell_text(r: int, c: int) -> str:
        if c == 1:
            return f"{r:02d}"
        if c == 2:
            return f"MAT-{1000 + r:04d}"
        if c == 3:
            return names[(r - 1) % len(names)]
        if c == 4:
            return specs_col[(r - 1) % len(specs_col)]
        if c == 5:
            return str(20 + (r * 7) % 480)
        if c == 6:
            return "件"
        if c == 7:
            return f"{12.5 + (r % 17) * 0.35:.2f}"
        if c == 8:
            qty = 20 + (r * 7) % 480
            return f"{(12.5 + (r % 17) * 0.35) * qty:,.2f}"
        if c == 9:
            return "13%"
        if c == 10:
            return whs[(r - 1) % len(whs)]
        if c == 11:
            return f"B26{(r * 13) % 1000:03d}"
        return "急件" if r % 9 == 0 else ""

    for r in range(1, rows + 1):
        row_top = y
        c = 1
        while c <= cols:
            merged = merges.get((r, c))
            if merged:
                span, text, align = merged
                x0, x1 = xs[c - 1], xs[min(cols, c + span - 1)]
                is_total = (r == rows)
                draw.rectangle([x0, row_top, x1, row_top + row_h],
                               fill=head_fill if is_total else None,
                               outline=ink, width=1)
                cy = row_top + row_h // 2
                if align == "l":
                    _draw_runs(draw, x0 + 8, cy, text, "text", px)
                else:
                    w = _measure(text, "text", px)
                    _draw_runs(draw, int((x0 + x1 - w) // 2), cy, text, "text", px)
                texts.append(text)
                c += span
                continue
            if r == rows:                 # 整行已由上面的合并格画完
                c += 1
                continue
            text = _cell_text(r, c)
            draw.rectangle([xs[c - 1], row_top, xs[c], row_top + row_h],
                           outline=ink, width=1)
            if text:
                cy = row_top + row_h // 2
                if c in (1, 5, 7, 8):     # 数字右对齐，更像真实报表
                    w = _measure(text, "text", px)
                    _draw_runs(draw, int(xs[c] - 8 - w), cy, text, "text", px)
                else:
                    _draw_runs(draw, xs[c - 1] + 6, cy, text, "text", px)
                texts.append(text)
            c += 1
        y += row_h
        lines += 1

    note = "注：本表由仓储系统导出，数量单位均为件，金额含 13% 增值税。"
    _draw_runs(draw, mx, y + 12, note, "text", 15, (90, 90, 90))
    texts.append(note)
    lines += 1
    return {"lines": lines, "texts": texts}


def render_math(draw, img, spec) -> dict:
    mx, my = spec["margin_x"], spec["margin_y"]
    body_px = spec["params"]["font_px"]
    form_px = spec["params"]["formula_font_px"]
    lh = spec["params"]["line_height"]
    max_w = img.width - 2 * mx
    bottom = img.height - my
    texts: list[str] = []
    lines = 0
    y = my
    for kind, text in CORPUS_MATH:
        if kind == "h1":
            size, gap, top = 40, 30, 26
        elif kind == "h2":
            size, gap, top = 32, 18, 18
        elif kind == "formula":
            size, gap, top = form_px, 16, 10
        else:
            size, gap, top = body_px, 10, 0
        y += top
        wrapped = _wrap(text, "text", size, max_w)
        for line in wrapped:
            if y + lh > bottom:
                break
            x = mx + int(max_w * 0.06) if kind == "formula" else mx
            _draw_rich(draw, x, y, line, size)
            texts.append(line)
            y += lh
            lines += 1
        y += gap
        if y + lh > bottom:
            break
    # 版面允许时用"定理-证明"补满底部（公式页本就留白较多，但别留 40%）
    extra = [
        ("h2", "3.9　定理与证明"),
        ("body", "定理：若函数列 {f_n} 在闭区间上一致收敛，则极限函数连续。"),
        ("formula", "∀ε>0 ∃N ∀n>N ∀x∈[a,b]：|f_n(x) − f(x)| < ε"),
        ("body", "证明：由一致收敛性取 N，再利用连续性在逐点上取值，三角不等式放缩即可。"),
        ("formula", "|f(x) − f(x₀)| ≤ |f(x) − f_n(x)| + |f_n(x) − f_n(x₀)| + |f_n(x₀) − f(x₀)|"),
        ("body", "推论：一致收敛的连续函数列可以逐项积分，且极限与积分可交换次序。"),
        ("formula", "lim_{n→∞} ∫_{a}^{b} f_n(x) dx = ∫_{a}^{b} lim_{n→∞} f_n(x) dx"),
        ("body", "注：以上证明省略了若干技术细节，仅用于制造密集的数学排版。"),
    ]
    for kind, text in extra:
        if y + lh > bottom:
            break
        size = 32 if kind == "h2" else body_px if kind == "body" else form_px
        y += 18 if kind == "h2" else 10
        x = mx + int(max_w * 0.06) if kind == "formula" else mx
        if y + lh > bottom:
            break
        _draw_rich(draw, x, y, text, size)
        texts.append(text)
        y += lh
        lines += 1
    return {"lines": lines, "texts": texts}


def render_two_column(draw, img, spec) -> dict:
    mx, my = spec["margin_x"], spec["margin_y"]
    title_px = spec["params"]["title_px"]
    body_px = spec["params"]["body_px"]
    abs_px = spec["params"]["abstract_px"]
    gutter = spec["params"]["gutter_px"]
    f_title = _font(FONT_CJK_BOLD, title_px)
    f_author = _font(FONT_CJK, 22)
    lh_cn, lh_en = _line_height(body_px), _line_height(body_px + 1)
    full_w = img.width - 2 * mx
    col_w = (full_w - gutter) // 2
    texts: list[str] = []
    lines = 0
    y = my

    title = "多模态文档版面理解中的阅读顺序恢复"
    draw.text((img.width // 2, y), title, font=f_title, fill=(0, 0, 0),
              anchor="mt")
    texts.append(title)
    lines += 1
    y += _line_height(title_px, 1.3)
    authors = "王 某¹　李 某²　Zhang San¹　（1. 某某研究院　2. 某某大学）"
    draw.text((img.width // 2, y), authors, font=f_author, fill=(40, 40, 40),
              anchor="mt")
    texts.append(authors)
    lines += 1
    y += _line_height(22, 1.6)

    abstract = ("摘　要：版面理解的目标是在保持阅读顺序的前提下，把扫描页上的文字、表格与"
                "图注映射为结构化的文档表示。本文提出一种基于区域约束的阅读顺序恢复方法，"
                "并在密集正文、双栏排版与跨页表格三类版式上验证其有效性。")
    n, y = _draw_paragraph(draw, mx, y, abstract, "text", abs_px, full_w,
                           _line_height(abs_px))
    texts.append(abstract)
    lines += n
    y += 6
    keywords = "关键词：版面分析；阅读顺序；文档理解；OCR；密集排版"
    _draw_runs(draw, mx, y, keywords, "text", abs_px, (30, 30, 30))
    texts.append(keywords)
    lines += 1
    y += _line_height(abs_px, 1.7)
    draw.line([mx, y, mx + full_w, y], fill=(120, 120, 120), width=1)

    col_top = y + 18
    col_bottom = img.height - my
    mid_x = mx + col_w
    draw.line([mid_x + gutter // 2, col_top, mid_x + gutter // 2, col_bottom],
              fill=(170, 170, 170), width=1)

    # 左栏：中文，填满到 col_bottom
    order = shuffled(CORPUS_ZH, SEED + 1)
    yy = col_top
    i = 0
    while yy + lh_cn <= col_bottom and i < len(order) * 2:
        para = order[(i * 2) % len(order)] + order[(i * 2 + 1) % len(order)]
        n, yy = _draw_paragraph(draw, mx, yy, para, "text", body_px, col_w,
                                lh_cn, bottom=col_bottom)
        texts.append(para)
        lines += n
        yy += 8
        i += 1

    # 右栏：英文（衬线），填满到 col_bottom
    order_en = shuffled(CORPUS_EN, SEED + 2)
    yy = col_top
    i = 0
    while yy + lh_en <= col_bottom and i < len(order_en) * 2:
        para = order_en[(i * 2) % len(order_en)] + " " + \
            order_en[(i * 2 + 1) % len(order_en)]
        n, yy = _draw_paragraph(draw, mx + col_w + gutter, yy, para, "serif",
                                body_px + 1, col_w, lh_en, bottom=col_bottom)
        texts.append(para)
        lines += n
        yy += 8
        i += 1
    return {"lines": lines, "texts": texts}


def _draw_code_block(draw, x: int, y: int, width: int, title: str, code: str,
                     mono_px: int, texts: list[str]) -> tuple[int, int]:
    """画一个带行号的代码块，返回 (下一行 y, 代码行数)。"""
    f_title = _font(FONT_CJK, 20)
    f_mono = _font(FONT_MONO, mono_px)
    code_lh = _line_height(mono_px, 1.5)
    draw.text((x, y), title, font=f_title, fill=(60, 60, 60))
    texts.append(title)
    y0 = y + _line_height(20, 1.6)
    code_lines = code.rstrip("\n").split("\n")
    box_h = len(code_lines) * code_lh + 24
    draw.rectangle([x, y0, x + width, y0 + box_h], fill=(247, 247, 247),
                   outline=(120, 120, 120), width=1)
    gutter_w = f_mono.getlength("00") + 18
    for i, line in enumerate(code_lines, 1):
        ly = y0 + 12 + (i - 1) * code_lh
        draw.text((x + 8, ly), f"{i:02d}", font=f_mono, fill=(130, 130, 130))
        _draw_runs(draw, x + gutter_w, ly, line if line else " ", "mono",
                   mono_px, (20, 20, 20))
        texts.append(line)
    return y0 + box_h + 10, len(code_lines)


def render_code_mix(draw, img, spec) -> dict:
    mx, my = spec["margin_x"], spec["margin_y"]
    body_px = spec["params"]["body_px"]
    mono_px = spec["params"]["mono_px"]
    max_w = img.width - 2 * mx
    lh = _line_height(body_px)
    bottom = img.height - my
    texts: list[str] = []
    lines = 0
    y = my
    order_en = shuffled(CORPUS_EN, SEED + 3)
    order_zh = shuffled(CORPUS_ZH, SEED + 4)

    def para(text: str, role: str, size: int) -> int:
        nonlocal y, lines
        n, y = _draw_paragraph(draw, mx, y, text, role, size, max_w, lh,
                               bottom=bottom)
        texts.append(text)
        lines += n
        y += 10
        return n

    para(order_en[0] + " " + order_en[1], "serif", body_px)
    para(order_zh[0] + order_zh[1], "text", body_px)
    y, n = _draw_code_block(draw, mx, y, max_w,
                            "def plan_groups(pages, merge_limit):  # 代码块 1",
                            CODE_SAMPLE_1, mono_px, texts)
    lines += n + 1
    y += 6
    para(order_zh[2] + order_zh[3], "text", body_px)
    y, n = _draw_code_block(draw, mx, y, max_w, "class Budget:  # 代码块 2",
                            CODE_SAMPLE_2, mono_px, texts)
    lines += n + 1
    y += 6
    i = 4
    while y + lh <= bottom and i < len(order_zh):
        para(order_zh[i] + order_zh[(i + 1) % len(order_zh)], "text", body_px)
        i += 2
    return {"lines": lines, "texts": texts}


def render_long_shot(draw, img, spec) -> dict:
    mx, my = spec["margin_x"], spec["margin_y"]
    px = spec["params"]["font_px"]
    lh = spec["params"]["line_height"]
    max_w = img.width - 2 * mx
    bottom = img.height - my
    texts: list[str] = []
    lines = 0
    y = my
    order = shuffled(CORPUS_ZH, SEED + 5)

    def heading(text: str, size: int, top: int = 14) -> None:
        nonlocal y, lines
        y += top
        if y + lh > bottom:
            return
        _draw_runs(draw, mx, y, text, "text", size, (20, 20, 20))
        texts.append(text)
        y += _line_height(size, 1.55)
        lines += 1

    heading("产品使用手册（节选）", px + 6, top=0)
    sections = ["一、安装与初始化", "二、配置项说明", "三、常见问题",
                "四、性能与资源占用", "五、版本与兼容性", "六、附录：术语表"]
    idx = 0
    for sec in sections:
        if y + lh > bottom:
            break
        heading(sec, px + 4)
        for _ in range(4):
            if y + lh > bottom:
                break
            # 长截图模拟网页正文：一段一到两句，避免整段过长显得不像网页
            para = order[idx % len(order)]
            if idx % 3 == 0:
                para += order[(idx + 11) % len(order)]
            idx += 1
            n, y = _draw_paragraph(draw, mx, y, para, "text", px, max_w, lh,
                                   bottom=bottom)
            texts.append(para)
            lines += n
            y += 4
        if spec["params"].get("bullets"):
            for b in range(3):
                if y + lh > bottom:
                    break
                bullet = "· " + order[(idx + b) % len(order)]
                n, y = _draw_paragraph(draw, mx + 10, y, bullet, "text",
                                       px - 2, max_w - 10, lh - 2, bottom=bottom)
                texts.append(bullet)
                lines += n
            idx += 3
        y += 8
    # 填满底部（长截图不应有 40% 空白，否则测不出长页真实输出）
    i = idx
    while y + lh <= bottom and i < idx + len(order) * 2:
        para = order[i % len(order)] + order[(i + 5) % len(order)]
        i += 1
        n, y = _draw_paragraph(draw, mx, y, para, "text", px, max_w, lh,
                               bottom=bottom)
        texts.append(para)
        lines += n
        y += 4
    return {"lines": lines, "texts": texts}


_RENDERERS = {
    "dense_text": render_dense_text,
    "table": render_table,
    "math": render_math,
    "two_column": render_two_column,
    "code_mix": render_code_mix,
    "long_shot": render_long_shot,
}


# ══════════════════════════════════════════════════════════════════
# 字形自检（--audit）：逐字渲染，与 tofu 参考位图比对
# ══════════════════════════════════════════════════════════════════
def _all_corpus_texts() -> list[tuple[str, str]]:
    """(文本, 角色) 列表：覆盖所有会被画进图里的字符来源。"""
    items: list[tuple[str, str]] = []
    items += [(t, "text") for t in CORPUS_ZH]
    items += [(t, "serif") for t in CORPUS_EN]
    items += [(t, "text") for _kind, t in CORPUS_MATH]
    items += [(t, "mono") for t in (CODE_SAMPLE_1, CODE_SAMPLE_2)]
    for spec in DOC_SPECS:
        items.append((spec["title"], "text"))
        items.append((spec["note"], "text"))
    return items


def audit_glyphs(size: int = 28) -> list[dict]:
    """返回会渲染成 tofu 的字符列表（空列表＝字形全覆盖）。需要 Pillow。

    判定：把字符画进 96×96 画布，与"必定缺字形"的非字符 U+10FFFF 的位图
    比较；相同即视为缺字形（Pillow 无法直接查 cmap，这是实测可行的替代）。
    """
    _require_pil()
    from PIL import Image, ImageDraw
    tofu_cache: dict = {}

    def render(font_path: str, size_: int, ch: str) -> bytes:
        img = Image.new("L", (96, 96), 0)
        ImageDraw.Draw(img).text((4, 4), ch, font=_font(font_path, size_), fill=255)
        return img.tobytes()

    def tofu_for(path: str) -> bytes:
        if path not in tofu_cache:
            tofu_cache[path] = render(path, size, "\U0010FFFF")
        return tofu_cache[path]

    seen: set = set()
    missing: list[dict] = []
    for text, role in _all_corpus_texts():
        for ch in str(text):
            if ch.isspace() or ch in seen:
                continue
            seen.add(ch)
            font_obj = _font_for_char(ch, role, size)
            path = getattr(font_obj, "path", "") or FONT_CJK
            if render(path, size, ch) == tofu_for(path):
                missing.append({"char": ch, "codepoint": f"U+{ord(ch):04X}",
                                "font": Path(path).name, "role": role})
    return missing


# ══════════════════════════════════════════════════════════════════
# 生成与落盘
# ══════════════════════════════════════════════════════════════════
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_json(path: Path, data: dict):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n")


def render_document(spec: dict, out_dir: Path) -> dict:
    """画一份文档 → PNG + meta.json，返回 meta。需要 Pillow。"""
    _require_pil()
    from PIL import Image, ImageDraw
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (spec["width"], spec["height"]), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    renderer = _RENDERERS.get(spec["kind"])
    if renderer is None:  # pragma: no cover - 配置写错时立刻暴露
        raise ValueError(f"unknown doc kind: {spec['kind']}")
    counts = renderer(draw, img, spec)
    png = out_dir / f"{spec['id']}.png"
    img.save(png, format="PNG", optimize=True)
    meta = build_meta(spec, lines=counts["lines"],
                      char_stats=text_stats(counts["texts"]),
                      params=spec.get("params"), image=img.size)
    meta["png"] = {"file": png.name, "bytes": png.stat().st_size,
                   "sha256": _sha256(png)}
    _write_json(out_dir / f"{spec['id']}.meta.json", meta)
    return meta


def generate(out_dir, only=None, verbose: bool = True) -> list[dict]:
    """生成（默认全部 6 份）。only: {"D1","D2"} 之类；返回 meta 列表。"""
    wanted = {str(x).strip().upper() for x in (only or []) if str(x).strip()}
    specs = [s for s in DOC_SPECS if not wanted or s["id"] in wanted]
    if wanted:
        missing = wanted - {s["id"] for s in specs}
        if missing:
            raise ValueError(f"未知文档 id：{', '.join(sorted(missing))}")
    out = Path(out_dir)
    metas = []
    for spec in specs:
        meta = render_document(spec, out)
        metas.append(meta)
        if verbose:
            img = meta["image"]
            c = meta["content"]
            print(f"{meta['id']}  {img['width']}x{img['height']}  "
                  f"h/w={img['hw_ratio']:.2f}  {c['lines']} 行  "
                  f"{c['chars_total']} 字  {meta['png']['bytes'] // 1024} KB  "
                  f"→ {out / meta['file']}")
    _write_json(out / "index.json", {
        "schema": "ocrmini/calib-index@1",
        "generator": {"script": GENERATOR, "version": DOC_VERSION},
        "docs": [{k: m[k] for k in ("id", "kind", "title", "file", "image",
                                    "content", "params", "png")} for m in metas],
    })
    return metas


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="gen_calib_docs.py",
        description="生成 D1–D6 密集校准文档（Pillow + 系统字体，不跑推理）")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录")
    ap.add_argument("--only", default="", help="只生成指定文档，如 D2,D6")
    ap.add_argument("--list", action="store_true", help="列出文档规格后退出")
    ap.add_argument("--audit", action="store_true", help="字形自检（不落盘）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.list:
        for s in DOC_SPECS:
            print(f"{s['id']}  {s['width']}x{s['height']}  {s['title']}  {s['note']}")
        return 0
    if args.audit:
        try:
            missing = audit_glyphs()
        except RuntimeError as e:
            print(f"[错误] {e}", file=sys.stderr)
            return 2
        if missing:
            for item in missing:
                print(f"[缺字形] {item['codepoint']} {item['char']!r} "
                      f"font={item['font']} role={item['role']}")
            return 1
        print(f"字形自检通过：{len(_unique_chars())} 个唯一字符全部可渲染")
        return 0
    only = [x for x in args.only.split(",") if x.strip()] if args.only else None
    try:
        generate(args.out, only=only, verbose=not args.quiet)
    except RuntimeError as e:           # Pillow 缺失等环境问题
        print(f"[错误] {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 1
    return 0


def _unique_chars() -> set:
    chars: set = set()
    for text, _role in _all_corpus_texts():
        chars.update(ch for ch in str(text) if not ch.isspace())
    return chars


if __name__ == "__main__":
    sys.exit(main())
