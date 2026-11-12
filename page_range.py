# -*- coding: utf-8 -*-
"""page_range.py — PDF 页范围解析（GUI 设置 / 引擎 / CLI 共用的唯一实现）

语法（1-based）：
  • `"5"`      单页
  • `"1-3"`    闭区间
  • `"10-"`    开区间（到末页；**没有总页数时无法解析** → ValueError）
  • 逗号分隔任意组合，全角逗号 `，` 等价；空串/None = 不筛选（返回 []）

越界夹取：给定 `total_pages` 时，单页号与区间端点都夹到 `[1, total_pages]`，
夹取产生的重复页由去重吸收（`"1-3,5" + total=3` → `[1, 2, 3]`）。

与 cli.py 的历史关系：本模块是 cli.parse_pages / cli._page_int 的原样迁移
（错误消息逐字保留，`tests/test_cli.py` 的 52 例依赖其行为）；cli.parse_pages
现在只是把 ValueError 转成 CliError 的薄包装。引擎侧直接用本模块，避免 GUI
与 CLI 两套页范围语义漂移。

设计约定：只抛 ValueError（面向库调用者）；需要本地化文案的调用方自行包装
（引擎包成 `EngineError(tr("err.badPageRange", spec=...))`，CLI 包成 CliError）。
"""
from __future__ import annotations

__all__ = ["parse_pages", "is_valid_spec", "split_tokens"]


def _page_int(token: str) -> int:
    try:
        return int(token)
    except (TypeError, ValueError):
        raise ValueError(f"not an integer: {token!r}") from None


def split_tokens(spec: str | None) -> list[str]:
    """把 spec 拆成 token 列表（去空白、全角逗号归一、丢弃空项）。"""
    if spec is None:
        return []
    text = str(spec).strip()
    if not text:
        return []
    return [t for t in (raw.strip() for raw in text.replace("，", ",").split(","))
            if t]


def parse_pages(spec: str | None, total_pages: int | None = None) -> list[int]:
    """解析 `"1-3,5,10-"` → 升序去重的 1-based 页号列表。

    • 空串/None → []（表示"不筛选"）
    • 开区间 `"10-"` 必须给 `total_pages`，否则 ValueError
    • 给定 `total_pages` 时越界夹取（端点与单页号都夹到 [1, total]，
      `total_pages < 1` 时返回 []）
    • 语法错误一律 ValueError（文案与旧 cli 实现逐字一致）
    """
    if spec is None:
        return []
    text = str(spec).strip()
    if not text:
        return []
    picked: set[int] = set()
    for token in split_tokens(text):
        if "-" in token:
            head, _, tail = token.partition("-")
            head, tail = head.strip(), tail.strip()
            if not head:
                raise ValueError(f"missing start in {token!r}")
            start = _page_int(head)
            if start < 1:
                raise ValueError(f"bad range {token!r}")
            if tail == "":
                if total_pages is None:
                    raise ValueError(
                        f"open range {token!r} needs total page count")
                end = int(total_pages)
            else:
                end = _page_int(tail)
                if end < start:
                    raise ValueError(f"bad range {token!r}")
            if total_pages is not None:
                if total_pages < 1:
                    continue
                # 越界夹取：开区间起点 > 总页数时夹到最后一页（"10-" + 5 页 → [5]）
                start = min(start, int(total_pages))
                end = min(max(end, start), int(total_pages))
            picked.update(range(start, end + 1))
        else:
            page = _page_int(token)
            if page < 1:
                raise ValueError(f"page must be >= 1: {token!r}")
            if total_pages is not None:
                if total_pages < 1:
                    continue
                page = min(page, int(total_pages))
            picked.add(page)
    return sorted(picked)


def is_valid_spec(spec: str | None) -> bool:
    """只校验**语法**（不要求知道总页数）：`"10-"` 也算合法。

    实现上用 `total_pages=1` 试解析：语法错误（非整数/区间倒置/页号 < 1）
    照样会抛；开区间与越界夹取都无害。空串/None 视为合法（= 不筛选）。
    """
    try:
        parse_pages(spec, total_pages=1)
        return True
    except ValueError:
        return False
