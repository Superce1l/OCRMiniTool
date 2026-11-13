# -*- coding: utf-8 -*-
"""office_export.py — stdlib 手写最小 OOXML 的 docx / xlsx 写入器（E9 / E10）

背景：本工具是「拖图片/PDF → 结构化结果」的本地小工具，导出 Word/Excel 时
不想为此引入 python-docx / openpyxl（两者都带 lxml 等原生依赖，会让 PyInstaller
发行包变大）。因此这里用 **标准库**（zipfile + xml.sax.saxutils + html.parser）
直接拼最小可用的 OOXML 包：

  docx_bytes(pages, *, title="") -> bytes     # 标题/正文/表格/分页符/core 属性
  xlsx_bytes(pages, *, name="") -> bytes      # 只导表格，一个表格一个 sheet
  write_docx(path, pages, *, title="") -> Path
  write_xlsx(path, pages, *, name="") -> Path

输入契约（与 ocr_postprocess.build_document() 的输出一致，E2）：

    pages = [{"page": 1, "blocks": [
        {"type": "title", "label": "title", "level": 1,
         "bbox": [...], "text": "# 第一章 …", "mergePrev": False},
        {"type": "text",  "label": "text",  "level": None, "text": "正文 **加粗**"},
        {"type": "table", "label": "table", "level": None,
         "text": "<table><tr><td colspan=\"2\">…</td></tr></table>"},
    ]}, …]

也接受 build_document() 返回的整包 dict（`{"pages": [...]}`）——两种形态都出现在
调用方代码里，归一化在 `_iter_pages()` 一处完成。

实现约定（也是降级点，测试里逐条覆盖）：

  • 标题：`#` 数量 → Heading 1..3（超过 3 级压到 3）；没有 `#` 时用 block.level
    兜底；正文/单元格里的 Markdown 行内标记（`**`、`*`、`` ` ``、链接、图片）先
    剥离再写入，导出的是**可读文本**而不是 Markdown 源码。
  • 表格：html.parser 解析（含 `colspan`/`rowspan`，属性值非法按 1 处理）；
    docx 用 `w:gridSpan` 表达 colspan；**rowspan 在 docx 里是保守近似**——文字只放
    在起始单元格，后续行用空单元格占位（不写 `w:vMerge`，避免半吊子合并导致
    Word 修复提示）；xlsx 则完整表达 `colspan+rowspan`（先建 mergeCells 再写值）。
  • 图片块（type=image/figure/chart/seal）：**跳过**——本模块不嵌入图片二进制，
    也不生成指向不存在文件的假链接（GUI 侧已有占位说明）。
  • 页与页之间插分页符；空页/空文本块不产生内容。
  • 包时间戳固定（`_ZIP_DATE`），同样输入得到同样字节，便于比对与回归。
  • 只依赖标准库；python-docx / openpyxl 只在 tests/ 里做**校验**，本模块不 import。
"""
from __future__ import annotations

import datetime
import html
import io
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.sax.saxutils import escape

__all__ = ["docx_bytes", "xlsx_bytes", "write_docx", "write_xlsx"]

# 固定的 zip 成员时间戳（同一输入 → 同一字节，便于回归比对）
_ZIP_DATE = (2020, 1, 1, 0, 0, 0)

# 这些 block 类型不导出（图片不嵌二进制，也不做假链接）
_IMAGE_TYPES = {"image", "figure", "chart", "seal", "picture", "img"}

_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.*)$")
_IMG_MD_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_MD_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_CODE_MD_RE = re.compile(r"`([^`]+)`")
_BOLD_MD_RE = re.compile(r"(\*\*|__)(.+?)\1")
_ITALIC_MD_RE = re.compile(r"(?<!\*)\*(?!\*)([^*]+?)(?<!\*)\*(?!\*)")
_HTML_TAG_RE = re.compile(r"<[^>]{1,200}>")

_XML_BAD_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


# ══════════════════════════════════════════════════════════════════
# 文本清洗 / 输入归一
# ══════════════════════════════════════════════════════════════════
def _clean_text(value) -> str:
    """把任意值变成可安全写进 XML 的文本（去非法控制字符、去孤立代理块）。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return _XML_BAD_RE.sub("", value)


def _esc(text) -> str:
    return escape(_clean_text(text), {'"': "&quot;"})


def strip_markdown(text: str) -> str:
    """去掉行内 Markdown 标记，返回可读纯文本（不处理块级结构）。"""
    s = _clean_text(text)
    if not s:
        return ""
    s = _IMG_MD_RE.sub("", s)
    s = _LINK_MD_RE.sub(r"\1", s)
    s = _CODE_MD_RE.sub(r"\1", s)
    s = _BOLD_MD_RE.sub(r"\2", s)
    s = _ITALIC_MD_RE.sub(r"\1", s)
    s = _HTML_TAG_RE.sub("", s)
    return html.unescape(s).replace("\r\n", "\n").replace("\r", "\n").strip()


def _iter_pages(pages) -> list[dict]:
    """接受 [页信封] 或 build_document() 的 {"pages": [...]}，统一成列表。"""
    if pages is None:
        return []
    if isinstance(pages, dict):
        pages = pages.get("pages") or []
    try:
        items = list(pages)
    except TypeError:
        return []
    return [p for p in items if isinstance(p, dict)]


def _iter_blocks(page: dict) -> list[dict]:
    blocks = page.get("blocks") or []
    try:
        items = list(blocks)
    except TypeError:
        return []
    return [b for b in items if isinstance(b, dict)]


def _block_kind(block: dict) -> str:
    kind = block.get("type") or block.get("label") or ""
    return str(kind).strip().lower()


def _heading_level(block: dict, text: str) -> int:
    """标题级别：优先 `#` 数量，其次 block.level，最后 1；夹到 1..3。"""
    m = _HEADING_RE.match(text or "")
    if m:
        level = len(m.group(1))
    else:
        raw = block.get("level")
        try:
            level = int(raw) if raw is not None else 1
        except (TypeError, ValueError):
            level = 1
    return max(1, min(3, level))


def _heading_text(text: str) -> str:
    m = _HEADING_RE.match(text or "")
    body = m.group(2) if m else (text or "")
    return strip_markdown(body)


# ══════════════════════════════════════════════════════════════════
# HTML 表格解析（stdlib html.parser）
# ══════════════════════════════════════════════════════════════════
class _Cell:
    __slots__ = ("tag", "colspan", "rowspan", "text")

    def __init__(self, tag: str, colspan: int = 1, rowspan: int = 1):
        self.tag = tag
        self.colspan = colspan
        self.rowspan = rowspan
        self.text = ""


class _TableParser(HTMLParser):
    """只认表格结构；单元格内的 <br> 变换行，其余标签丢弃、实体解码。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_Cell]] = []
        self._depth = 0
        self._row: list[_Cell] | None = None
        self._cell: _Cell | None = None
        self._buf: list[str] = []

    # -- helpers ------------------------------------------------
    @staticmethod
    def _span(attrs, name: str) -> int:
        for key, value in attrs:
            if (key or "").lower() == name:
                try:
                    return max(1, int(str(value).strip()))
                except (TypeError, ValueError):
                    return 1
        return 1

    def handle_starttag(self, tag, attrs):
        tag = (tag or "").lower()
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self.rows = []
            return
        if self._depth != 1:
            return
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = _Cell(tag, self._span(attrs, "colspan"),
                               self._span(attrs, "rowspan"))
            self._buf = []
        elif tag == "br" and self._cell is not None:
            self._buf.append("\n")

    def handle_startendtag(self, tag, attrs):
        if (tag or "").lower() == "br" and self._cell is not None:
            self._buf.append("\n")

    def handle_data(self, data):
        if self._cell is not None and data:
            self._buf.append(data)

    def handle_endtag(self, tag):
        tag = (tag or "").lower()
        if tag == "table":
            self._depth = max(0, self._depth - 1)
            return
        if self._depth != 1:
            return
        if tag in ("td", "th") and self._cell is not None:
            raw = "".join(self._buf)
            self._cell.text = _cell_text(raw)
            if self._row is not None:
                self._row.append(self._cell)
            self._cell = None
            self._buf = []
        elif tag == "tr":
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _cell_text(raw: str) -> str:
    """单元格文本：去标签、解码实体、行内 Markdown 剥离、压缩空白。"""
    text = _HTML_TAG_RE.sub("", raw)
    text = html.unescape(text)
    text = strip_markdown(text)
    lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def parse_table(html_text: str) -> list[list[_Cell]]:
    """HTML → 行 × 单元格（保留 colspan/rowspan 原始值）。"""
    parser = _TableParser()
    try:
        parser.feed(_clean_text(html_text))
        parser.close()
    except Exception:
        return []
    return parser.rows


class _Grid:
    """把 rowspan/colspan 展开成矩形网格；格位分 origin / span / vspan。"""

    ORIGIN, SPAN, VSPAN = "origin", "span", "vspan"

    def __init__(self, rows: list[list[_Cell]]):
        self.ncols = 0
        self.nrows = 0
        self.cells: dict[tuple[int, int], tuple[_Cell, int, int]] = {}
        self.slots: dict[tuple[int, int], str] = {}
        occupied: set[tuple[int, int]] = set()
        for r, row in enumerate(rows):
            c = 0
            for cell in row:
                while (r, c) in occupied:
                    c += 1
                cs = max(1, int(cell.colspan or 1))
                rs = max(1, int(cell.rowspan or 1))
                self.cells[(r, c)] = (cell, cs, rs)
                for dr in range(rs):
                    for dc in range(cs):
                        key = (r + dr, c + dc)
                        occupied.add(key)
                        if dr == 0 and dc == 0:
                            self.slots[key] = self.ORIGIN
                        elif dr == 0:
                            self.slots[key] = self.SPAN
                        else:
                            self.slots[key] = self.VSPAN
                c += cs
            self.ncols = max(self.ncols, c)
        for key in occupied:
            self.ncols = max(self.ncols, key[1] + 1)
            self.nrows = max(self.nrows, key[0] + 1)
        if rows:
            self.nrows = max(self.nrows, len(rows))

    def merges(self) -> list[tuple[int, int, int, int]]:
        """(r1, c1, r2, c2)，0-based 闭区间；只收录真正跨格子的单元格。"""
        out = []
        for (r, c), (_cell, cs, rs) in sorted(self.cells.items()):
            if cs > 1 or rs > 1:
                out.append((r, c, r + rs - 1, c + cs - 1))
        return out


# ══════════════════════════════════════════════════════════════════
# DOCX（WordprocessingML）
# ══════════════════════════════════════════════════════════════════
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

_DOCX_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="' + _CT_NS + '">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
    'package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    '<Override PartName="/docProps/core.xml" ContentType="application/vnd.'
    'openxmlformats-package.core-properties+xml"/>'
    '</Types>'
)

_DOCX_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="' + _PKG_REL_NS + '">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
    'officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/'
    'package/2006/relationships/metadata/core-properties" '
    'Target="docProps/core.xml"/>'
    '</Relationships>'
)

_DOCX_DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="' + _PKG_REL_NS + '">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
    'officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    '</Relationships>'
)

# 最小样式表：Normal + Heading 1..3（python-docx / Word 都认这套内建名）
_DOCX_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<w:styles xmlns:w="' + _W_NS + '">'
    '<w:docDefaults><w:rPrDefault><w:rPr>'
    '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="SimSun"/>'
    '<w:sz w:val="21"/><w:szCs w:val="21"/>'
    '</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>'
    '<w:spacing w:after="120" w:line="276" w:lineRule="auto"/>'
    '</w:pPr></w:pPrDefault></w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
    '<w:name w:val="Normal"/><w:qFormat/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1">'
    '<w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
    '<w:next w:val="Normal"/><w:qFormat/>'
    '<w:pPr><w:keepNext/><w:spacing w:before="240" w:after="120"/>'
    '<w:outlineLvl w:val="0"/></w:pPr>'
    '<w:rPr><w:b/><w:sz w:val="32"/><w:szCs w:val="32"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading2">'
    '<w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>'
    '<w:next w:val="Normal"/><w:qFormat/>'
    '<w:pPr><w:keepNext/><w:spacing w:before="200" w:after="100"/>'
    '<w:outlineLvl w:val="1"/></w:pPr>'
    '<w:rPr><w:b/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading3">'
    '<w:name w:val="heading 3"/><w:basedOn w:val="Normal"/>'
    '<w:next w:val="Normal"/><w:qFormat/>'
    '<w:pPr><w:keepNext/><w:spacing w:before="160" w:after="80"/>'
    '<w:outlineLvl w:val="2"/></w:pPr>'
    '<w:rPr><w:b/><w:sz w:val="24"/><w:szCs w:val="24"/></w:rPr></w:style>'
    '</w:styles>'
)

_SECT_PR = (
    '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
    '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"'
    ' w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>'
)


def _core_xml(title: str) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    title_el = f"<dc:title>{_esc(title)}</dc:title>" if title else ""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<cp:coreProperties'
        ' xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
        'core-properties"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:dcterms="http://purl.org/dc/terms/"'
        ' xmlns:dcmitype="http://purl.org/dc/dcmitype/"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        + title_el +
        '<dc:creator>OCRMiniTool</dc:creator>'
        '<cp:lastModifiedBy>OCRMiniTool</cp:lastModifiedBy>'
        '<dcterms:created xsi:type="dcterms:W3CDTF">' + stamp +
        '</dcterms:created>'
        '<dcterms:modified xsi:type="dcterms:W3CDTF">' + stamp +
        '</dcterms:modified>'
        '</cp:coreProperties>'
    )


def _para(text: str = "", style: str | None = None) -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    if not text:
        return f"<w:p>{ppr}</w:p>"
    return (f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{_esc(text)}</w:t>'
            '</w:r></w:p>')


def _para_page_break() -> str:
    return ('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')


def _cell_paragraphs(text: str) -> str:
    if not text:
        return "<w:p/>"
    parts = [p for p in str(text).split("\n")]
    out = []
    for part in parts:
        if part:
            out.append(f'<w:p><w:r><w:t xml:space="preserve">{_esc(part)}</w:t>'
                       '</w:r></w:p>')
        else:
            out.append("<w:p/>")
    return "".join(out)


def _docx_table_xml(rows: list[list[_Cell]]) -> str:
    """行 × 格 → `w:tbl`；colspan→gridSpan；rowspan 用空单元格占位（保守）。"""
    grid = _Grid(rows)
    if not grid.ncols:
        return ""
    col_w = max(1200, int(9000 / max(1, grid.ncols)))
    grid_cols = "".join(f'<w:gridCol w:w="{col_w}"/>' for _ in range(grid.ncols))
    body = []
    for r in range(grid.nrows):
        tcs = []
        for c in range(grid.ncols):
            kind = grid.slots.get((r, c))
            if kind == _Grid.SPAN:
                continue                     # 被本行前一个 colspan 覆盖
            if kind == _Grid.VSPAN:
                tcs.append("<w:tc><w:tcPr/><w:p/></w:tc>")
                continue
            origin = grid.cells.get((r, c))
            if origin is None:
                tcs.append("<w:tc><w:tcPr/><w:p/></w:tc>")
                continue
            cell, cs, _rs = origin
            tcpr = f'<w:tcPr><w:gridSpan w:val="{cs}"/></w:tcPr>' if cs > 1 \
                else "<w:tcPr/>"
            head = '<w:rPr><w:b/></w:rPr>' if cell.tag == "th" else ""
            if head:
                content = "".join(
                    f'<w:p><w:r>{head}<w:t xml:space="preserve">{_esc(part)}'
                    f'</w:t></w:r></w:p>' if part else "<w:p/>"
                    for part in str(cell.text).split("\n"))
            else:
                content = _cell_paragraphs(cell.text)
            tcs.append(f"<w:tc>{tcpr}{content}</w:tc>")
        body.append("<w:tr>" + "".join(tcs) + "</w:tr>")
    borders = (
        '<w:tblBorders>'
        '<w:top w:val="single" w:sz="4" w:space="0" w:color="808080"/>'
        '<w:left w:val="single" w:sz="4" w:space="0" w:color="808080"/>'
        '<w:bottom w:val="single" w:sz="4" w:space="0" w:color="808080"/>'
        '<w:right w:val="single" w:sz="4" w:space="0" w:color="808080"/>'
        '<w:insideH w:val="single" w:sz="4" w:space="0" w:color="808080"/>'
        '<w:insideV w:val="single" w:sz="4" w:space="0" w:color="808080"/>'
        '</w:tblBorders>'
    )
    return (
        '<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/>' + borders +
        '</w:tblPr><w:tblGrid>' + grid_cols + '</w:tblGrid>' +
        "".join(body) + "</w:tbl>"
    )


def _docx_body(pages) -> str:
    chunks: list[str] = []
    first = True
    for page in _iter_pages(pages):
        blocks = _iter_blocks(page)
        if not blocks:
            continue
        if not first:
            chunks.append(_para_page_break())
        first = False
        for block in blocks:
            kind = _block_kind(block)
            text = block.get("text")
            raw = text if isinstance(text, str) else ("" if text is None
                                                      else str(text))
            if kind in _IMAGE_TYPES:
                continue                     # 不嵌图片、不造假链接
            if kind == "table":
                rows = parse_table(raw)
                table = _docx_table_xml(rows)
                if table:
                    chunks.append(table)
                    chunks.append("<w:p/>")  # 表格后补空段，Word 更稳
                continue
            # 标题优先看 `#`（渲染器一定带）；手工构造的块可能只有 type=title，
            # 此时用 block.level 兜底（见 _heading_level）。
            heading_match = _HEADING_RE.match(raw.strip())
            if kind == "title" or heading_match:
                body = _heading_text(raw) if heading_match else strip_markdown(raw)
                if body:
                    chunks.append(_para(
                        body, f"Heading{_heading_level(block, raw)}"))
                continue
            body = strip_markdown(raw)
            if body:
                chunks.append(_para(body))
    return "".join(chunks)


def docx_bytes(pages, *, title: str = "") -> bytes:
    """结构化 pages → 最小可用 .docx 字节（纯 stdlib，无第三方依赖）。"""
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="' + _W_NS + '" xmlns:r="' + _R_NS + '">'
        '<w:body>' + _docx_body(pages) + _SECT_PR +
        '</w:body></w:document>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        def put(name: str, data: str):
            info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data.encode("utf-8"))

        put("[Content_Types].xml", _DOCX_CONTENT_TYPES)
        put("_rels/.rels", _DOCX_ROOT_RELS)
        put("word/document.xml", document)
        put("word/_rels/document.xml.rels", _DOCX_DOC_RELS)
        put("word/styles.xml", _DOCX_STYLES)
        put("docProps/core.xml", _core_xml(title or ""))
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════
# XLSX（SpreadsheetML）
# ══════════════════════════════════════════════════════════════════
_S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

_XLSX_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<styleSheet xmlns="' + _S_NS + '">'
    '<fonts count="1"><font><sz val="11"/><name val="Calibri"/>'
    '</font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border/></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" '
    'borderId="0"/></cellStyleXfs>'
    '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"'
    ' xfId="0"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/>'
    '</cellStyles><dxfs count="0"/><tableStyles count="0"/></styleSheet>'
)

_NUM_RE = re.compile(r"^[+-]?(?:\d+|\d*\.\d+)$")


def _col_name(index: int) -> str:
    """0-based 列号 → Excel 列名（0→A, 25→Z, 26→AA）。"""
    name = ""
    n = int(index) + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        name = chr(65 + rem) + name
    return name


def _cell_value(text: str):
    """纯数字串 → (数值, 浮点)；否则 ('inlineStr', 文本)。"""
    s = (text or "").strip()
    if s and _NUM_RE.match(s):
        try:
            return "n", float(s)
        except ValueError:
            pass
    return "inlineStr", text or ""


def _xml_cell(ref: str, value: str) -> str:
    kind, val = _cell_value(value)
    if kind == "n":
        return f'<c r="{ref}"><v>{val:g}</v></c>'
    if val == "":
        return f'<c r="{ref}"/>'
    return (f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">'
            f'{_esc(val)}</t></is></c>')


def _col_widths(grid: _Grid) -> str:
    widths: dict[int, int] = {}
    for (r, c), (cell, _cs, _rs) in grid.cells.items():
        for line in str(cell.text).split("\n"):
            visual = sum(2 if ord(ch) > 0x2E80 else 1 for ch in line)
            widths[c] = max(widths.get(c, 0), visual)
    if not widths:
        return ""
    cols = []
    for c in range(grid.ncols):
        w = min(60.0, max(8.0, widths.get(c, 0) * 1.15))
        cols.append(f'<col min="{c + 1}" max="{c + 1}" width="{w:.1f}" '
                    'customWidth="1"/>')
    return "<cols>" + "".join(cols) + "</cols>"


def _sheet_xml(rows: list[list[_Cell]]) -> str:
    grid = _Grid(rows)
    if not grid.ncols or not grid.nrows:
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<worksheet xmlns="' + _S_NS + '"><sheetData/>'
                '</worksheet>')
    row_xml = []
    for r in range(grid.nrows):
        cells = []
        for c in range(grid.ncols):
            if grid.slots.get((r, c)) != _Grid.ORIGIN:
                continue                     # 合并区域的非左上角不写值
            cell, _cs, _rs = grid.cells[(r, c)]
            cells.append(_xml_cell(f"{_col_name(c)}{r + 1}", cell.text))
        row_xml.append(f'<row r="{r + 1}">' + "".join(cells) + "</row>")
    last = f"{_col_name(grid.ncols - 1)}{grid.nrows}"
    merges = grid.merges()
    merge_xml = ""
    if merges:
        items = []
        for r1, c1, r2, c2 in merges:
            items.append(f'<mergeCell ref="{_col_name(c1)}{r1 + 1}:'
                         f'{_col_name(c2)}{r2 + 1}"/>')
        merge_xml = (f'<mergeCells count="{len(items)}">'
                     + "".join(items) + "</mergeCells>")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<worksheet xmlns="' + _S_NS + '">'
        f'<dimension ref="A1:{last}"/>'
        + _col_widths(grid) +
        "<sheetData>" + "".join(row_xml) + "</sheetData>" +
        merge_xml +
        "</worksheet>"
    )


def _table_blocks(pages) -> list[list[list[_Cell]]]:
    """按页序收集所有 table 块（解析为空表/无行的跳过）。"""
    tables = []
    for page in _iter_pages(pages):
        for block in _iter_blocks(page):
            if _block_kind(block) != "table":
                continue
            raw = block.get("text")
            rows = parse_table(raw if isinstance(raw, str) else "")
            if rows:
                tables.append(rows)
    return tables


def xlsx_bytes(pages, *, name: str = "") -> bytes:
    """结构化 pages → 最小可用 .xlsx 字节（只导表格；无表格抛 ValueError）。

    每个 table 块一个工作表，显示名为「表格1」「表格2」…；`name` 只写进
    core 属性（文档名），不影响工作表命名（Excel 表名有 31 字符/禁用字符限制，
    固定编号更稳）。
    """
    tables = _table_blocks(pages)
    if not tables:
        raise ValueError("no tables")
    n = len(tables)
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.worksheet+xml"/>' for i in range(n))
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="' + _CT_NS + '">'
        '<Default Extension="rels" ContentType="application/vnd.'
        'openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        + overrides +
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.'
        'openxmlformats-package.core-properties+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="' + _PKG_REL_NS + '">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/'
        'package/2006/relationships/metadata/core-properties" '
        'Target="docProps/core.xml"/>'
        '</Relationships>'
    )
    sheets = "".join(
        f'<sheet name="表格{i + 1}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
        for i in range(n))
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<workbook xmlns="' + _S_NS + '" xmlns:r="' + _R_NS + '">'
        '<bookViews><workbookView activeTab="0"/></bookViews>'
        '<sheets>' + sheets + '</sheets></workbook>'
    )
    rels = [
        f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{i + 1}.xml"/>' for i in range(n)
    ]
    rels.append('<Relationship Id="rId' + str(n + 1) + '" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/styles" Target="styles.xml"/>')
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="' + _PKG_REL_NS + '">'
        + "".join(rels) + '</Relationships>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        def put(name_: str, data: str):
            info = zipfile.ZipInfo(name_, date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data.encode("utf-8"))

        put("[Content_Types].xml", content_types)
        put("_rels/.rels", root_rels)
        put("xl/workbook.xml", workbook)
        put("xl/_rels/workbook.xml.rels", workbook_rels)
        put("xl/styles.xml", _XLSX_STYLES)
        put("docProps/core.xml", _core_xml(name or ""))
        for i, rows in enumerate(tables):
            put(f"xl/worksheets/sheet{i + 1}.xml", _sheet_xml(rows))
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════
# 写盘
# ══════════════════════════════════════════════════════════════════
def write_docx(path, pages, *, title: str = "") -> Path:
    """把 docx_bytes() 写到 path（直接写；原子性由调用方负责）。"""
    target = Path(path)
    target.write_bytes(docx_bytes(pages, title=title))
    return target


def write_xlsx(path, pages, *, name: str = "") -> Path:
    """把 xlsx_bytes() 写到 path（直接写；原子性由调用方负责）。"""
    target = Path(path)
    target.write_bytes(xlsx_bytes(pages, name=name))
    return target
