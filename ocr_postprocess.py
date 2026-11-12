"""ocr_postprocess.py — Unlimited-OCR-2 原始输出 → Markdown

模型不直接输出 Markdown，而是布局块序列：

    <PAGE><BLOCK>title<LEVEL1>[x1,y1,x2,y2]<FORK>标题文字
    <BLOCK>text[x1,y1,x2,y2]<FORK>正文……
    <BLOCK>cross_table[bbox]<FORK><table>…</table><PAGE><BLOCK>append_table[bbox]<FORK>…

本模块移植自上游 baidu/Unlimited-OCR 的 utils/process_unlimitedOCR2.py（MIT），
转换规则：
  * 按 <PAGE> 分页；title<LEVELn> → n 级标题（缺省 1 级）
  * image/figure/chart → 图片占位（GUI 模式转斜体说明，不生成假链接）
  * table/append_table → HTML 表格；跨页表格片段合并（去重表头）
  * cross_/append_/<MERGE> 链按并查集聚组、阅读序拼接
"""
from __future__ import annotations

import re

# ── block / token patterns ───────────────────────────────────────────────────
PAGE_SPLIT_RE = re.compile(r'<\s*/?\s*PAGE\s*\d*\s*>', re.I)
MERGE_RE = re.compile(r'<\s*/?\s*MERGE\s*>', re.I)
STRAY_TOKEN_RE = re.compile(
    r'<[|｜]/?[a-zA-Z_]+[|｜]>|<image>'
    r'|<\s*/?\s*PAGE\s*\d*\s*>|<\s*/?\s*BLOCK\s*>|<\s*/?\s*FORK\s*>'
    r'|<\s*/?\s*LEVEL\s*\d*\s*[>}]?|<\s*/?\s*MERGE\s*>', re.I)
BLOCK_RE = re.compile(
    r'<\s*BLOCK\s*>\s*([a-zA-Z_]+)'
    r'(?:'
    r'\s*<\s*/?\s*LEVEL\s*(\d*)\s*[>}]?(?:\s*\[\s*(-?\d+)\s*[,，]\s*(-?\d+)\s*[,，]\s*(-?\d+)\s*[,，]\s*(-?\d+)'
    r'(?:\s*[,，]\s*-?\d+)*\s*\])?'
    r'|'
    r'\s*\[\s*(-?\d+)\s*[,，]\s*(-?\d+)\s*[,，]\s*(-?\d+)\s*[,，]\s*(-?\d+)'
    r'(?:\s*[,，]\s*-?\d+)*\s*\](?:\s*<\s*/?\s*LEVEL\s*(\d*)\s*[>}]?)?'
    r')?'
    r'\s*(?:<\s*FORK\s*>)?', re.I)
TR_RE = re.compile(r'<tr\b[^>]*>.*?</tr>', re.I | re.S)
TABLE_OPEN_RE = re.compile(r'<table\b[^>]*>', re.I)
SPAN_ATTR_RE = re.compile(r'\b(colspan|rowspan)\s*=\s*"([^"]*)"', re.I)

DROP_LABELS = set()
# E11：可选丢弃的「版式噪声」标签。默认**不丢**（DROP_LABELS 保持空集合不动，
# 既有 convert()/to_gui_markdown() 行为完全不变）；只有调用方显式传
# drop_headers=True 时才丢弃这三类，并回报丢弃块数。
DROP_LABELS_OPT_IN = {'header', 'footer', 'page_number'}
IMAGE_LABELS = {'image', 'figure', 'picture', 'chart', 'img', 'seal'}
NOISE_CONTENT = {'[non-text]', '[unreadable]', '[image]', '[figure]'}
CHAIN_TYPES = {'text', 'table'}
MAX_HEADER_ROWS = 5

_BASE_LABELS = {
    'text', 'title', 'header', 'footer', 'page_number', 'image', 'figure',
    'chart', 'seal', 'table', 'image_caption', 'image_footnote',
    'page_footnote', 'equation', 'ref_text', 'aside_text', 'algorithm',
}
_BARE_LABELS = _BASE_LABELS | {
    f'{p}{l}' for p in ('cross_', 'append_') for l in _BASE_LABELS}
BARE_BLOCK_RE = re.compile(
    r'(?:(?<=<FORK>)|(?<=<PAGE>)|(?<=\n)|^)'
    r'(?:<[|｜]\s*det\s*[|｜]>)?'
    r'((?:[a-zA-Z_][a-zA-Z_0-9]*)(?:\s*<\s*/?\s*LEVEL\s*\d*\s*[>}]?)?\s*'
    r'\[\s*-?\d+(?:\s*[,，]\s*-?\d+){3,}[^\]]*\])', re.I)

# GUI 图片占位：不在预览里渲染指向不存在文件的假链接
_IMG_PLACEHOLDER_RE = re.compile(r'!\[\]\(page(\d+)_(\d+)_(\d+)_(\d+)_(\d+)\.jpg\)')


def _scale(v):
    return max(0, min(1000, int(v)))


def _fix_span_attrs(text):
    def repl(m):
        name, val = m.group(1), m.group(2)
        try:
            int(val)
            return m.group(0)
        except ValueError:
            num = re.search(r'\d+', val)
            return f'{name}="{num.group() if num else 1}"'
    return SPAN_ATTR_RE.sub(repl, text)


def _fix_bare_blocks(text):
    """Add the missing <BLOCK> prefix to bare block declarations (known labels only)."""
    def _repl(m):
        label = re.match(r'[a-zA-Z_][a-zA-Z_0-9]*', m.group(1)).group(0)
        if label.lower() not in _BARE_LABELS:
            return m.group(0)
        return f'<BLOCK>{m.group(1)}'
    text = BARE_BLOCK_RE.sub(_repl, text)
    return re.sub(r'\n\s*<FORK>\s*(?=<BLOCK>)', '\n', text, flags=re.I)


def _base_type(label):
    """cross_text/append_text -> text; append_table/cross_table -> table."""
    for p in ('append_', 'cross_'):
        if label.startswith(p):
            return label[len(p):]
    return label


def _parse_blocks(text, page_base=1):
    """Split on <PAGE>; return blocks [{label,bbox,level,content,page,merge_prev}]."""
    text = _fix_bare_blocks(text)
    segments = PAGE_SPLIT_RE.split(text)
    if segments and not segments[0].strip():
        segments = segments[1:]
    blocks = []
    for i, seg in enumerate(segments):
        page_no = page_base + i
        if not seg.strip():
            continue
        matches = list(BLOCK_RE.finditer(seg))
        if not matches:
            blocks.append({'label': '__raw__', 'bbox': None, 'level': None,
                           'content': MERGE_RE.sub('', seg).strip(),
                           'page': page_no, 'merge_prev': False})
            continue
        for j, m in enumerate(matches):
            gap = seg[:m.start()] if j == 0 else seg[matches[j - 1].end():m.start()]
            merge_prev = bool(MERGE_RE.search(gap))
            if j == 0:
                pre = STRAY_TOKEN_RE.sub('', MERGE_RE.sub('', gap)).strip()
                if pre:
                    blocks.append({'label': '__raw__', 'bbox': None, 'level': None,
                                   'content': pre, 'page': page_no, 'merge_prev': False})
            end = matches[j + 1].start() if j + 1 < len(matches) else len(seg)
            content = MERGE_RE.sub('', seg[m.end():end]).strip()
            if m.group(2) is not None:          # LEVEL-first
                level = int(m.group(2)) if m.group(2) else None
                bbox = (tuple(int(m.group(k)) for k in range(3, 7))
                        if m.group(3) is not None else None)
            else:                                # bbox-first (or none)
                bbox = (tuple(int(m.group(k)) for k in range(7, 11))
                        if m.group(7) is not None else None)
                level = int(m.group(11)) if m.group(11) else None
            blocks.append({'label': m.group(1).lower(), 'bbox': bbox, 'level': level,
                           'content': content, 'page': page_no, 'merge_prev': merge_prev})
    return blocks


def _merge_adjacent_titles(blocks):
    """Merge strictly-adjacent, same-page, same-<LEVELn> title blocks into one."""
    if not blocks:
        return blocks
    out = [blocks[0]]
    for blk in blocks[1:]:
        prev = out[-1]
        if (blk['label'] == 'title' and prev['label'] == 'title'
                and blk['page'] == prev['page']
                and blk['level'] is not None and prev['level'] is not None
                and blk['level'] == prev['level']):
            prev['content'] = '\n'.join(x for x in (prev['content'], blk['content']) if x)
            continue
        out.append(blk)
    return out


def _build_groups(blocks):
    """Union-find grouping of append/cross chains and same-page <MERGE> columns."""
    n = len(blocks)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    pages_with = {}
    for blk in blocks:
        b = _base_type(blk['label'])
        if blk['label'] != '__raw__' and b in CHAIN_TYPES:
            pages_with.setdefault(b, set()).add(blk['page'])

    def _table_adjacent(tail_page, cur_page):
        if cur_page - tail_page <= 1:
            return True
        return all(p not in pages_with.get('table', ())
                   for p in range(tail_page + 1, cur_page))

    open_chain = {}
    for i, blk in enumerate(blocks):
        label, base = blk['label'], _base_type(blk['label'])
        if label == '__raw__' or base not in CHAIN_TYPES:
            continue
        if label.startswith('append_'):
            tail = open_chain.get(base)
            if tail is None:
                continue
            if base == 'table' and not _table_adjacent(blocks[tail]['page'], blk['page']):
                open_chain.pop(base, None)
                continue
            union(tail, i)
            open_chain[base] = i
        elif label.startswith('cross_'):
            open_chain[base] = i
        elif base == 'table':
            open_chain[base] = i

    for i, blk in enumerate(blocks):       # same-page <MERGE> column edges
        if not blk.get('merge_prev') or i == 0:
            continue
        pblk = blocks[i - 1]
        if pblk['page'] != blk['page']:
            continue
        if (blk['label'] == 'title' or _base_type(blk['label']) == 'text') \
                and (pblk['label'] == 'title' or _base_type(pblk['label']) == 'text'):
            union(i - 1, i)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return [groups[r] for r in sorted(groups, key=lambda r: groups[r][0])]


def _row_key(tr_html):
    txt = re.sub(r'<[^>]+>', ' ', tr_html)
    txt = ''.join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in txt)
    ncells = len(re.findall(r'<t[hd]\b', tr_html, re.I))
    return (ncells, ''.join(txt.split()).lower())


def _merge_table_fragments(frags):
    """Merge cross-page table fragments, skipping repeated leading header rows."""
    frags = [f for f in frags if f and f.strip()]
    if len(frags) <= 1:
        return frags[0] if frags else ''
    base_rows = TR_RE.findall(frags[0])
    if not base_rows:
        return '\n\n'.join(frags)
    out_rows, base_keys, extra = list(base_rows), [_row_key(r) for r in base_rows], []
    for frag in frags[1:]:
        rows = TR_RE.findall(frag)
        if not rows:
            extra.append(frag.strip())
            continue
        skip = 0
        for r in rows[:MAX_HEADER_ROWS]:
            if skip < len(base_keys) and _row_key(r) == base_keys[skip]:
                skip += 1
            else:
                break
        out_rows.extend(rows[skip:])
    m = TABLE_OPEN_RE.search(frags[0])
    merged = (m.group(0) if m else '<table>') + ''.join(out_rows) + '</table>'
    return merged + ('\n\n' + '\n\n'.join(extra) if extra else '')


def _strip_outer_fence(text):
    """去掉模型输出偶尔带的 ```markdown 外层围栏（convert 与 build_document 共用）。"""
    text = (text or '').strip()
    if text.startswith('```markdown'):
        text = text[len('```markdown'):].strip()
    if text.endswith('```'):
        text = text[:-3].strip()
    return text


def _render_group(blocks, members, drop_headers=False, stats=None):
    """把一个渲染组转成 {text,label,type,level,bbox,mergePrev}；丢弃/空 → None。

    这是 Markdown 渲染与结构化 blocks 的**唯一**事实来源：`_render_blocks`
    只取 text，`build_document` 取全部字段——两者因此不可能各自漂移。
    """
    head = blocks[members[0]]
    label = head['label']
    base = label if label == '__raw__' else _base_type(label)
    contents = [blocks[k]['content'] for k in members
                if blocks[k]['content'] and blocks[k]['content'].lower() not in NOISE_CONTENT]
    text = None
    type_name = 'text' if base == '__raw__' else base
    if base == '__raw__':
        if contents:
            text = contents[0]
    elif base in IMAGE_LABELS:
        if contents:
            text = contents[0]
    elif not contents or base in DROP_LABELS:
        return None
    elif drop_headers and base in DROP_LABELS_OPT_IN:
        # E11：页眉/页脚/页码——只在调用方显式要求时丢，且计入统计
        if stats is not None:
            stats['droppedBlocks'] = stats.get('droppedBlocks', 0) + 1
        return None
    elif base == 'title':
        lv = max(1, head['level'] or 1)
        joined = ''.join(contents)
        if not STRAY_TOKEN_RE.sub('', joined).strip():
            return None
        text = f'{"#" * lv} {joined}'
    elif base == 'table':
        text = _merge_table_fragments(contents) if len(contents) > 1 else contents[0]
    else:
        text = ''.join(contents)          # text family: concatenate, no separator
    if text is None:
        return None
    bbox = head.get('bbox')
    return {'text': text,
            'label': label,
            'type': type_name,
            'level': head.get('level'),
            'bbox': list(bbox) if bbox else None,
            # 组内任一成员带 <MERGE> 就算「该块由列合并而来」
            'mergePrev': any(bool(blocks[k].get('merge_prev')) for k in members)}


def _render_blocks(blocks, groups, drop_headers=False, stats=None):
    """Render each group to a Markdown item (figure/table/heading/text)."""
    out = []
    for members in groups:
        item = _render_group(blocks, members, drop_headers, stats)
        if item is not None:
            out.append(item['text'])
    return out


def _head_level(item):
    s = item.lstrip()
    return len(s) - len(s.lstrip('#'))


def _single_root_titles(items):
    """Collapse multiple top-level headings under the first one (single root)."""
    idxs = [i for i, it in enumerate(items) if it.lstrip().startswith('#')]
    if len(idxs) <= 1:
        return items
    levels = [_head_level(items[i]) for i in idxs]
    min_lv = min(levels)
    if sum(1 for lv in levels if lv == min_lv) <= 1:
        return items
    first_lv, out = levels[0], list(items)
    for k, i in enumerate(idxs):
        body = items[i].lstrip()[levels[k]:].lstrip(' ')
        new_lv = 1 if k == 0 else max(2, levels[k] - first_lv + 2)
        out[i] = '#' * new_lv + ' ' + body
    return out


def _fold_head_ws(items):
    out = []
    for it in items:
        s = it.lstrip()
        if s.startswith('#'):
            n = len(s) - len(s.lstrip('#'))
            it = '#' * n + ' ' + re.sub(r'\s+', ' ', s[n:]).strip()
        out.append(it)
    return out


def _drop_empty_heads(items):
    return [it for it in items
            if not (it.lstrip().startswith('#') and not it.lstrip().lstrip('#').strip())]


# ── 文本规整（E13 LaTeX / E12 去断行+中英空格 / E14 表格校验）──────
# 四个函数都是「纯文本进、纯文本出」的独立纯函数：不依赖 layout 块结构，
# 可单独测试与复用。管线顺序见 _finalize_text()（两条入口共用）。
_FENCE_RE = re.compile(r'^\s*```')
_INLINE_CODE_RE = re.compile(r'`[^`\n]*`')
_LATEX_INLINE_RE = re.compile(r'\\\((.+?)\\\)', re.S)
_LATEX_BLOCK_RE = re.compile(r'\\\[(.+?)\\\]', re.S)
_BLANK_LINE_RE = re.compile(r'\n[ \t]*\n')


def _map_outside_code(text, fn):
    """对 ``` 围栏之外的片段应用 fn；围栏内的行原样保留。"""
    out, buf, in_fence = [], [], False

    def _flush(is_code):
        if buf:
            chunk = '\n'.join(buf)
            out.append(chunk if is_code else _map_outside_inline(chunk, fn))
            buf.clear()

    for line in text.split('\n'):
        if _FENCE_RE.match(line):
            _flush(in_fence)
            in_fence = not in_fence
            out.append(line)
            continue
        buf.append(line)
    _flush(in_fence)
    return '\n'.join(out)


def _map_outside_inline(chunk, fn):
    """对行内 `code` 之外的片段应用 fn（code 原样保留）。"""
    out, pos = [], 0
    for m in _INLINE_CODE_RE.finditer(chunk):
        out.append(fn(chunk[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(fn(chunk[pos:]))
    return ''.join(out)


def normalize_latex(text):
    """LaTeX 定界符归一：`\\(...\\)` → `$...$`、`\\[...\\]` → `$$...$$`。

    支持跨行块公式（`\\[ ... \\]` 内部可含换行）；跳过 ``` 代码围栏与行内
    `code`。幂等：转换后不再含这两个定界符，二次调用无变化。
    """
    if not text:
        return text or ''

    def _conv(chunk):
        def _block(m):
            inner = m.group(1)
            # 防御：跨空行的 `\[ ... \]` 不是块公式（多半是输出里孤立的转义
            # 方括号），原样保留，避免把整页吞进一个 $$ 里
            if _BLANK_LINE_RE.search(inner):
                return m.group(0)
            return '$$' + inner.strip() + '$$'

        def _inline(m):
            inner = m.group(1)
            if '\n' in inner:            # 行内公式不跨行，避免误吞大段正文
                return m.group(0)
            return '$' + inner.strip() + '$'

        chunk = _LATEX_BLOCK_RE.sub(_block, chunk)
        return _LATEX_INLINE_RE.sub(_inline, chunk)

    return _map_outside_code(text, _conv)


_SENTENCE_END = set('。！？；：、.!?;:,…') | set('」』）】》〉”’"\'')
_LIST_LINE_RE = re.compile(r'^(?:[-*+]\s|\d+[.)]\s)')


def _is_block_line(s):
    """该行是否为不可合并的结构行（标题/列表/表格/引用/HTML/围栏）。"""
    if not s:
        return True
    if s.startswith(('#', '|', '>', '<', '```')):
        return True
    if s.endswith('-->'):        # HTML 注释的收尾行（如 `-->` 单独成行）
        return True
    return bool(_LIST_LINE_RE.match(s))


def _ends_sentence(s):
    return bool(s) and s[-1] in _SENTENCE_END


def _join_pair(a, b):
    """合并两行：中文直接相连、英文（两侧都是 ASCII 字母数字）补一个空格。"""
    a, b = a.rstrip(), b.lstrip()
    if not a:
        return b
    if not b:
        return a
    if a[-1].isascii() and a[-1].isalnum() and b[0].isascii() and b[0].isalnum():
        return a + ' ' + b
    return a + b


def join_wrapped_lines(text):
    """把被硬换行拆开的普通段落连成一行（中文直接相连、英文间补一格空格）。

    不合并的安全边界：空行两侧、标题(#)、列表(-/*/+/数字.)、表格行(|)、
    引用(>)、代码围栏内、HTML 块（<div>/<table>/<!-- --> 等）、上一行以
    中英文句末标点结尾。幂等：合并后是普通单行，二次调用不再变化。
    """
    if not text:
        return text or ''
    out, in_fence, in_comment = [], False, False
    for raw in text.split('\n'):
        s = raw.strip()
        if s.startswith('```'):
            in_fence = not in_fence
            out.append(raw)
            continue
        if in_fence:
            out.append(raw)
            continue
        if in_comment:
            out.append(raw)
            if '-->' in s:
                in_comment = False
            continue
        head = s.split('<!--', 1)[1] if '<!--' in s else None
        if head is not None and '-->' not in head:
            out.append(raw)
            in_comment = True
            continue
        if not s:
            out.append('')
            continue
        if _is_block_line(s):
            out.append(raw)
            continue
        if out:
            prev = out[-1]
            ps = prev.strip()
            if ps and prev == ps and not _is_block_line(ps) \
                    and not _ends_sentence(ps):
                out[-1] = _join_pair(prev, s)
                continue
        out.append(raw)
    return '\n'.join(out)


_CJK_CLASS = '[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]'
_CJK_LATIN_RE = re.compile('(?<=' + _CJK_CLASS + ')(?=[A-Za-z0-9])')
_LATIN_CJK_RE = re.compile('(?<=[A-Za-z0-9])(?=' + _CJK_CLASS + ')')
_PROTECT_INLINE_RE = re.compile(
    r'`[^`\n]*`'                              # 行内 code
    r'|!?\[[^\]\n]*\]\([^)\n]*\)'             # Markdown 图片/链接语法
    r'|(?:https?://|www\.)[^\s<>)\]|]+'       # URL
    r'|<[^>\n]{1,200}>'                       # HTML 标签
)


def _space_cjk_latin_chunk(chunk):
    return _LATIN_CJK_RE.sub(' ', _CJK_LATIN_RE.sub(' ', chunk))


def space_cjk_latin(text):
    """在「汉字 ↔ 拉丁字母/数字」边界各插一个空格（已有的空格不动）。

    跳过：代码围栏/行内 `code`、URL（http(s)://、www.）、HTML 标签、
    markdown 表格行与 HTML 块行、图片/链接语法内部。幂等：插入后边界不再
    相邻，二次调用无变化。
    """
    if not text:
        return text or ''
    out, in_fence = [], False
    for raw in text.split('\n'):
        s = raw.strip()
        if s.startswith('```'):
            in_fence = not in_fence
            out.append(raw)
            continue
        if in_fence or s.startswith(('|', '<')):
            out.append(raw)
            continue
        parts, pos = [], 0
        for m in _PROTECT_INLINE_RE.finditer(raw):
            parts.append(_space_cjk_latin_chunk(raw[pos:m.start()]))
            parts.append(m.group(0))
            pos = m.end()
        parts.append(_space_cjk_latin_chunk(raw[pos:]))
        out.append(''.join(parts))
    return '\n'.join(out)


_SEP_CELL_RE = re.compile(r'^:?-{1,}:?$')


def _split_table_row(s):
    body = s.strip()
    if body.startswith('|'):
        body = body[1:]
    if body.endswith('|'):
        body = body[:-1]
    return body.split('|')


def _is_sep_row(cells):
    return bool(cells) and all(_SEP_CELL_RE.match(c.strip()) for c in cells)


def _table_block_is_bad(rows):
    """表格块内任一数据行列数 != 表头列数 → 1，否则 0（分隔行不计）。"""
    cells = [_split_table_row(r) for r in rows]
    sep = next((i for i, c in enumerate(cells) if _is_sep_row(c)), None)
    if sep is not None:
        header = cells[sep - 1] if sep > 0 else None
        data = cells[sep + 1:]
    else:
        header, data = (cells[0] if cells else None), cells[1:]
    if header is None or not data:
        return 0
    n = len(header)
    return 1 if any(len(r) != n for r in data) else 0


def check_tables(text):
    """统计列数不一致的 markdown 表格块数量（只计数、不改写）。

    表格块 = 连续以 `|` 开头的行；分隔行 `|---|` 不参与比对；
    数据行列数 != 表头列数即记 1（每块最多记 1）。
    """
    if not text:
        return 0
    count, block = 0, []
    for line in text.split('\n'):
        if line.strip().startswith('|'):
            block.append(line)
            continue
        if block:
            count += _table_block_is_bad(block)
            block = []
    if block:
        count += _table_block_is_bad(block)
    return count


def _finalize_text(md, join_lines=False, cjk_spacing=False, stats=None):
    """layout→Markdown 之后的文本管线（两条入口共用，顺序固定）。

    normalize_latex（始终）→ join_lines（开关）→ cjk_spacing（开关）
    → check_tables（始终，只计数写 stats['tableWarnings']）。
    drop_headers 在更早的 layout 渲染阶段处理（必须先于文本规整）。
    """
    text = normalize_latex(md)
    if join_lines:
        text = join_wrapped_lines(text)
    if cjk_spacing:
        text = space_cjk_latin(text)
    if stats is not None:
        stats['tableWarnings'] = check_tables(text)
    return text


def _convert_impl(result, drop_headers=False, stats=None,
                  join_lines=False, cjk_spacing=False):
    """convert() 的实现体：返回 Markdown 字符串，统计写进 stats。

    对外契约不变：`convert(result, ...) -> str`；需要统计时用
    `convert_with_stats()`。文本规整顺序见 _finalize_text()。
    """
    if stats is not None:
        stats.setdefault('droppedBlocks', 0)
        stats.setdefault('tableWarnings', 0)
    text = _strip_outer_fence(result)

    blocks = _parse_blocks(text, page_base=1)
    blocks = _merge_adjacent_titles(blocks)
    groups = _build_groups(blocks)
    items = _drop_empty_heads(_render_blocks(blocks, groups, drop_headers, stats))
    items = _fold_head_ws(items)
    text = '\n\n'.join(items)

    text = text.replace('\\coloneqq', ':=').replace('\\eqqcolon', '=:')
    text = STRAY_TOKEN_RE.sub('', text)
    text = _fix_span_attrs(text)
    text = re.sub(r'(?m)^\s*\[\s*-?\d+\s*,\s*-?\d+\s*,\s*-?\d+\s*,\s*-?\d+\s*\]\s*$', '', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    md = '\n\n'.join(_single_root_titles(text.split('\n\n')))
    return _finalize_text(md, join_lines, cjk_spacing, stats)


def convert(result, drop_headers=False, join_lines=False, cjk_spacing=False):
    """Unlimited-OCR 2 raw document string → Markdown（移植自上游 convert()）。

    契约不变：`convert(str) -> str`。drop_headers=True 时额外丢弃
    header/footer/page_number（E11）；join_lines=True 去掉硬换行（E12）；
    cjk_spacing=True 在汉字与拉丁/数字间补空格（E12）。
    LaTeX 定界符归一（E13）始终执行；表格列数校验只计数，不在此返回。
    """
    return _convert_impl(result, drop_headers, None, join_lines, cjk_spacing)


def convert_with_stats(result, drop_headers=False, join_lines=False,
                       cjk_spacing=False):
    """convert() 的带统计变体：返回 (markdown, stats)。

    stats 至少含：droppedBlocks（E11 丢弃块数）、tableWarnings（E14 列数不符
    的表格块数）。
    """
    stats = {'droppedBlocks': 0, 'tableWarnings': 0}
    md = _convert_impl(result, drop_headers, stats, join_lines, cjk_spacing)
    return md, stats


def to_gui_markdown(raw: str, drop_headers=False, join_lines=False,
                    cjk_spacing=False) -> str:
    """convert() + GUI 适配：图片占位 → 斜体区域说明（本工具不落盘裁剪图）。"""
    return to_gui_markdown_with_stats(raw, drop_headers, join_lines,
                                      cjk_spacing)[0]


def to_gui_markdown_with_stats(raw: str, drop_headers=False, join_lines=False,
                               cjk_spacing=False):
    """to_gui_markdown() 的带统计变体：返回 (markdown, stats)。

    管线：layout 块→Markdown（drop_headers 在此生效）→ normalize_latex（始终）
    → join_lines（开关）→ cjk_spacing（开关）→ check_tables（始终，仅计数）
    → GUI 图片占位替换。stats 含 droppedBlocks / tableWarnings。
    """
    stats = {'droppedBlocks': 0, 'tableWarnings': 0}
    md = _convert_impl(raw, drop_headers, stats, join_lines, cjk_spacing)

    def _img_repl(m):
        page, x1, y1, x2, y2 = m.groups()
        return f'*［图片区域 第{page}页 ({x1},{y1})-({x2},{y2})］*'
    return _IMG_PLACEHOLDER_RE.sub(_img_repl, md), stats


def build_document(raw_text, page_base=1, drop_headers=False):
    """模型原始输出 → 结构化文档 {"pages": [{"page", "blocks": [...]}]}（E2）。

    与 Markdown 渲染共用同一条管线：`_parse_blocks` → `_merge_adjacent_titles`
    → `_build_groups` → `_render_group`。因此 blocks 的顺序就是 Markdown 的
    阅读序，text 与渲染片段逐字一致（不存在"另写一套解析"导致的漂移）。

    每个 block：{"type": 归一类型（_base_type；__raw__ → "text"）,
                 "label": 原始小写 label,
                 "level": int|None（模型原始标题层级）,
                 "bbox": [x0,y0,x1,y1]|None（模型原始像素坐标）,
                 "text": 该块的渲染片段（Markdown/HTML）,
                 "mergePrev": bool（该块是否由 <MERGE> 列合并而来）}

    page_base: 起始绝对页号（引擎按组调用时传「已处理页数+1」）；纯函数，不读
    全局状态、不写文件。畸形输入降级为「整段一个 text 块」，不抛非预期异常。
    drop_headers=True 时排除 header/footer/page_number；某页块被清空则该页不
    出现在 pages 里。

    注：页面级收尾（LaTeX 归一 / 去断行 / 中英空格 / 单根标题收敛 / 尾随 bbox
    行清理）仍由 convert()/to_gui_markdown() 在**整页文本**上执行；这里的 text
    是同一渲染片段，因此与最终页面文本在这些页面级步骤上可能差一步。
    """
    try:
        base = max(1, int(page_base))
    except (TypeError, ValueError):
        base = 1
    text = _strip_outer_fence(raw_text)
    if not text:
        return {"pages": []}
    try:
        blocks = _merge_adjacent_titles(_parse_blocks(text, page_base=base))
        groups = _build_groups(blocks)
        pages = {}
        for members in groups:
            item = _render_group(blocks, members,
                                 drop_headers=bool(drop_headers))
            if item is None:
                continue
            page_no = blocks[members[0]]['page']
            pages.setdefault(page_no, []).append(item)
        return {"pages": [{"page": p, "blocks": pages[p]}
                          for p in sorted(pages)]}
    except Exception:      # 降级：解析异常不拖垮识别，整段当一个 text 块
        fallback = STRAY_TOKEN_RE.sub('', text).strip()
        if not fallback:
            return {"pages": []}
        return {"pages": [{"page": base, "blocks": [
            {"type": "text", "label": "__raw__", "level": None,
             "bbox": None, "text": fallback, "mergePrev": False}]}]}
