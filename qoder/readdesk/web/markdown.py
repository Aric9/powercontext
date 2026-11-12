"""极简离线 Markdown 渲染（仅用于简报页展示，全部输出先做 HTML 转义）。"""

from __future__ import annotations

import html
import re

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")


def _inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _INLINE_CODE.sub(r"<code>\1</code>", out)
    return out


def render_markdown(md: str) -> str:
    """把简报 Markdown 转为 HTML 片段（标题/列表/引用/表格/段落）。"""
    out: list[str] = []
    in_ul = in_ol = in_table = False
    for raw in md.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            lvl = len(m.group(1)) + 1  # 页面里降一级，避免与页面主标题冲突
            out.append(f"<h{min(lvl, 5)}>{_inline(m.group(2))}</h{min(lvl, 5)}>")
            in_ul = in_ol = in_table = False
            continue
        if stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                continue
            if not in_table:
                out.append("<table class='tbl'>")
                in_table = True
            out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if stripped.startswith("- ") or stripped.startswith("* "):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{_inline(stripped[2:])}</li>")
            continue
        if in_ul:
            out.append("</ul>")
            in_ul = False
        m = re.match(r"^(\d+)[.)]\s+(.*)$", stripped)
        if m:
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{_inline(m.group(2))}</li>")
            continue
        if in_ol:
            out.append("</ol>")
            in_ol = False
        if stripped.startswith(">"):
            out.append(f"<blockquote>{_inline(stripped.lstrip('>').strip())}</blockquote>")
            continue
        if not stripped:
            continue
        out.append(f"<p>{_inline(stripped)}</p>")
    for tag, flag in (("</ul>", in_ul), ("</ol>", in_ol), ("</table>", in_table)):
        if flag:
            out.append(tag)
    return "\n".join(out)
