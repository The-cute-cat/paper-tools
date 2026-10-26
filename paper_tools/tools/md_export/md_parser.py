"""Markdown 解析后端：markdown-it-py → 本项目渲染层约定的中间结构。

渲染层（docx / pdf）依赖两套结构，本模块负责产出它们：

* 块级 :func:`parse_markdown` → ``list[dict]``，
  kind ∈ heading / para / code / math / image / quote / list / table / hr
* 行内 :func:`_parse_inline` → ``list[Seg]``，
  kind ∈ text / code / math / img / br

为什么不自己写正则状态机
------------------------
CommonMark 的边界情况（嵌套强调、反斜杠转义、引用式链接、列表续行、表格列数
与对齐、缩进代码块）远超手写正则的合理维护范围。旧实现已知的两个问题：

* ``[^1]: 脚注内容`` 被引用式链接定义规则 ``[ref]: url`` 误吞，整行从正文消失，
  而正文里的 ``[^1]`` 又原样残留为纯文本；
* ``\\(x\\)``、``\\[x\\]``、``\\begin{equation}`` 是 arxiv 翻译稿里的常见写法，
  却要靠 ``_TOKEN_RE`` 逐个补分支维护。

因此语法解析交给 markdown-it-py（CommonMark 规范的 Python 实现），公式与脚注
由官方插件 ``dollarmath`` / ``texmath`` / ``footnote`` 补足。本模块只保留
「token 流 → 中间结构」的薄适配，不再包含任何 Markdown 语法知识。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.texmath import texmath_plugin

from ...core.exporter import _split_gfm_cells

# ──────────────────────────────────────────────
# 行内片段模型
# ──────────────────────────────────────────────


@dataclass
class Seg:
    """行内片段：kind ∈ text/code/math/img/br。"""
    kind: str
    text: str = ""          # 文本内容 / 公式 LaTeX 源码 / 图片 src
    bold: bool = False
    italic: bool = False
    strike: bool = False
    href: str = ""          # 链接目标（text 片段可能来自链接）
    alt: str = ""           # 图片 alt
    display: bool = False   # math 片段是否为 display 公式


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# 任务列表项：`- [ ] xxx` / `- [x] xxx`
_TASK_RE = re.compile(r"^\[([ xX])\]\s+")

# 行首编号（1. / 2) …）：LaTeX 提示词里常见「注意：1. … / 2. …」跨行编号，
# CommonMark 规定有序列表只有以 1 开头才能打断段落，2. 3. 会被并入上一段。
# 这类行仍按换行处理，避免多条编号指令糊成一行。
_ORDERED_LINE_RE = re.compile(r"^\d{1,3}[.)]\s")

# 行内 HTML：<br> 与 <img>
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_HTML_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
_ANY_TAG_RE = re.compile(r"<[^>]+>")


def _html_attr(tag: str, name: str) -> str:
    """从单个 HTML 标签里取属性值（无引号写法也支持）。"""
    m = re.search(
        rf"""{name}\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", tag, re.I)
    if not m:
        return ""
    return next((g for g in m.groups() if g is not None), "")


# ──────────────────────────────────────────────
# 解析器实例
# ──────────────────────────────────────────────

# equation / align 等 LaTeX 数学环境（\begin{...} ... \end{...} 独占多行）
_MATH_ENVS = {
    "equation", "equation*", "displaymath", "align", "align*", "aligned",
    "gather", "gather*", "multline", "multline*", "eqnarray", "eqnarray*",
    "split", "cases",
}


def _math_env_rule(state, start_line: int, end_line: int, silent: bool) -> bool:
    """块级规则：``\\begin{equation} ... \\end{equation}`` → math_block。

    markdown-it 默认不认识这套写法，会把整段当普通段落。这里剥掉外层环境
    包裹后交给 math_block，让 mathtext 能直接渲染公式本体。
    """
    begin = state.bMarks[start_line] + state.tShift[start_line]
    m = re.match(r"\\begin\{([A-Za-z]+\*?)\}", state.src[begin:begin + 48])
    if not m or m.group(1) not in _MATH_ENVS:
        return False
    env = m.group(1)
    end_tag = f"\\end{{{env}}}"
    end_pos = state.src.find(end_tag, begin)
    if end_pos < 0:
        return False
    last_line = start_line + state.src.count("\n", begin, end_pos)
    if last_line >= end_line:
        return False
    if silent:
        return True
    token = state.push("math_block", "math", 0)
    token.block = True
    token.content = state.src[begin + m.end():end_pos].strip()
    token.markup = "$$"
    state.line = last_line + 1
    return True


def _build_markdown_it() -> MarkdownIt:
    md = MarkdownIt("commonmark")
    # GFM 扩展：表格与删除线（CommonMark 本身不含）
    md.enable(["table", "strikethrough"])
    # $...$ / $$...$$：不把 $100 这类金额当公式
    md.use(dollarmath_plugin, allow_space=False, allow_digits=False)
    # \(...\) / \[...\]：arxiv 等 LaTeX 惯用写法
    md.use(texmath_plugin, delimiters="brackets")
    # [^1]: 脚注定义（否则会被当成引用式链接定义而丢行）
    md.use(footnote_plugin)
    md.block.ruler.before("fence", "math_env", _math_env_rule)
    return md


_MD = _build_markdown_it()


# ──────────────────────────────────────────────
# 行内解析
# ──────────────────────────────────────────────


def _html_inline_segs(raw: str, bold: bool, italic: bool, strike: bool,
                      href: str) -> list[Seg]:
    """把行内 HTML 标签翻译成片段：<br> → 换行，<img> → 图片，其余忽略。"""
    tag = raw.strip()
    if _BR_RE.fullmatch(tag):
        return [Seg("br")]
    m = _HTML_IMG_RE.fullmatch(tag)
    if m:
        src = _html_attr(m.group(0), "src")
        if src:
            return [Seg("img", src, bold, italic, strike, href,
                        alt=_html_attr(m.group(0), "alt"))]
    return []


def _walk_inline(children: list[Any], segs: list[Seg], bold: bool,
                 italic: bool, strike: bool, href: str) -> None:
    """遍历 inline token 的 children，累积为 Seg 列表。"""
    pending_space = False

    def tail_char() -> str:
        for seg in reversed(segs):
            if seg.kind == "text":
                if seg.text:
                    return seg.text[-1]
            elif seg.kind == "br":
                return "\n"
            else:
                return "x"   # 代码/公式/图片视为非 CJK
        return ""

    for tok in children:
        t = tok.type
        if t == "strong_open":
            bold = True
        elif t == "strong_close":
            bold = False
        elif t == "em_open":
            italic = True
        elif t == "em_close":
            italic = False
        elif t == "s_open":
            strike = True
        elif t == "s_close":
            strike = False
        elif t == "link_open":
            href = tok.attrGet("href") or ""
        elif t == "link_close":
            href = ""
        elif t == "text":
            content = tok.content
            if content:
                # 软换行：CJK 之间不加空格，编号行保留换行，其余补一个空格
                if pending_space:
                    if _ORDERED_LINE_RE.match(content):
                        segs.append(Seg("br"))
                    elif not (_CJK_RE.search(tail_char()) and _CJK_RE.search(content[0])):
                        content = " " + content
                    pending_space = False
                segs.append(Seg("text", content, bold, italic, strike, href))
        elif t == "softbreak":
            pending_space = True
        elif t == "hardbreak":
            pending_space = False
            segs.append(Seg("br"))
        elif t == "code_inline":
            segs.append(Seg("code", tok.content, bold, italic, strike))
        elif t in ("math_inline", "math_single"):
            segs.append(Seg("math", tok.content.strip(), bold, italic, strike))
        elif t in ("math_block", "math_block_eqno", "math_display"):
            segs.append(Seg("math", tok.content.strip(), bold, italic, strike,
                            display=True))
        elif t == "image":
            alt = (tok.content or tok.attrGet("alt") or "").strip()
            segs.append(Seg("img", tok.attrGet("src") or "", bold, italic, strike,
                            alt=alt))
        elif t == "html_inline":
            segs.extend(_html_inline_segs(tok.content, bold, italic, strike, href))
        elif t == "footnote_ref":
            # 脚注定义在文末统一输出，正文保留 [^n] 标记（与源文本一致）
            label = (tok.meta or {}).get("label", "")
            segs.append(Seg("text", f"[^{label}]", bold, italic, strike, href))


def _parse_inline(text: str, bold: bool = False, italic: bool = False,
                  strike: bool = False, href: str = "") -> list[Seg]:
    """把一行 Markdown 文本解析为行内片段列表（支持嵌套样式）。"""
    if not text:
        return []
    tokens = _MD.parseInline(text)
    if not tokens:
        return []
    segs: list[Seg] = []
    _walk_inline(tokens[0].children or [], segs, bold, italic, strike, href)
    return segs


# ──────────────────────────────────────────────
# 块级解析
# ──────────────────────────────────────────────


def _parse_table_aligns(sep_line: str, ncols: int) -> list[str]:
    """从 GFM 分隔行解析各列对齐：:--- 左 / :--: 居中 / ---: 右。"""
    cells = sep_line.strip().strip("|").split("|") if sep_line.strip() else []
    aligns: list[str] = []
    for c in cells:
        c = c.strip()
        if c.startswith(":") and c.endswith(":"):
            aligns.append("center")
        elif c.endswith(":"):
            aligns.append("right")
        else:
            aligns.append("left")
    while len(aligns) < ncols:
        aligns.append("left")
    return aligns[:ncols]


def _match_close(tokens: list[Any], i: int, open_type: str,
                 close_type: str) -> int:
    """找到与 tokens[i] 配对的关闭 token 下标（支持同类嵌套）。"""
    depth = 0
    for j in range(i, len(tokens)):
        if tokens[j].type == open_type:
            depth += 1
        elif tokens[j].type == close_type:
            depth -= 1
            if depth == 0:
                return j
    return len(tokens) - 1


def _drop_last_newline(s: str) -> str:
    return s[:-1] if s.endswith("\n") else s


def _image_block(src: str, alt: str) -> dict:
    return {"kind": "image", "src": src, "alt": alt}


def _parse_paragraph(tokens: list[Any], i: int, blocks: list[dict]) -> int:
    """段落：独占一张图片的段落提升为 image 块，其余保留原始行内文本。"""
    inline = tokens[i + 1]
    children = inline.children or []
    imgs = [
        c for c in children
        if c.type == "image"
        or (c.type == "html_inline" and _HTML_IMG_RE.fullmatch(c.content.strip()))
    ]
    rest = [
        c for c in children
        if c not in imgs and not (c.type == "text" and not c.content.strip())
    ]
    if len(imgs) == 1 and not rest:
        only = imgs[0]
        if only.type == "image":
            blocks.append(_image_block(only.attrGet("src") or "",
                                       (only.content or "").strip()))
        else:
            tag = only.content.strip()
            blocks.append(_image_block(_html_attr(tag, "src"),
                                       _html_attr(tag, "alt")))
        return i + 3
    blocks.append({"kind": "para", "text": inline.content})
    return i + 3


def _list_close_type(open_type: str) -> str:
    return "bullet_list_close" if open_type == "bullet_list_open" else "ordered_list_close"


def _parse_list(tokens: list[Any], i: int, items: list[dict],
                depth: int) -> int:
    """列表：输出扁平的 items 列表，depth 表示嵌套层级（最多两级）。"""
    open_type = tokens[i].type
    close_type = _list_close_type(open_type)
    ordered = open_type == "ordered_list_open"
    depth = min(2, depth)
    j = i + 1
    while j < len(tokens) and tokens[j].type != close_type:
        if tokens[j].type == "list_item_open":
            j = _parse_list_item(tokens, j, items, depth, ordered)
        else:
            j += 1
    return j + 1


def _parse_list_item(tokens: list[Any], i: int, items: list[dict],
                     depth: int, ordered: bool) -> int:
    j = i + 1
    parts: list[str] = []
    nested: list[int] = []
    while j < len(tokens) and tokens[j].type != "list_item_close":
        t = tokens[j]
        if t.type == "inline":
            parts.append(t.content)
            j += 1
        elif t.type in ("bullet_list_open", "ordered_list_open"):
            nested.append(j)
            j = _match_close(tokens, j, t.type, _list_close_type(t.type)) + 1
        else:
            j += 1

    text = " ".join(p.strip() for p in parts if p.strip())
    task: bool | None = None
    m = _TASK_RE.match(text)
    if m:
        task = m.group(1).lower() == "x"
        text = text[m.end():]
    items.append({"depth": depth, "ordered": ordered, "task": task, "text": text})

    # 子列表排在父项之后，保证输出顺序与源文档一致
    for k in nested:
        _parse_list(tokens, k, items, depth + 1)
    return j + 1


def _parse_blocks(tokens: list[Any], i: int, end: int,
                  lines: list[str]) -> tuple[list[dict], int]:
    blocks: list[dict] = []
    while i < end:
        tok = tokens[i]
        t = tok.type

        if t == "heading_open":
            blocks.append({
                "kind": "heading",
                "level": int(tok.tag[1:]),
                "text": tokens[i + 1].content,
            })
            i += 3

        elif t == "paragraph_open":
            i = _parse_paragraph(tokens, i, blocks)

        elif t == "fence":
            info = (tok.info or "").strip()
            blocks.append({
                "kind": "code",
                "text": _drop_last_newline(tok.content),
                "lang": info.split()[0] if info else "",
            })
            i += 1

        elif t == "code_block":
            blocks.append({
                "kind": "code",
                "text": _drop_last_newline(tok.content),
                "lang": "",
            })
            i += 1

        elif t == "math_block":
            blocks.append({"kind": "math", "text": tok.content.strip()})
            i += 1

        elif t == "hr":
            blocks.append({"kind": "hr"})
            i += 1

        elif t == "blockquote_open":
            close = _match_close(tokens, i, "blockquote_open", "blockquote_close")
            children, _ = _parse_blocks(tokens, i + 1, close, lines)
            blocks.append({"kind": "quote", "children": children})
            i = close + 1

        elif t in ("bullet_list_open", "ordered_list_open"):
            items: list[dict] = []
            i = _parse_list(tokens, i, items, 0)
            blocks.append({"kind": "list", "items": items})

        elif t == "table_open":
            close = _match_close(tokens, i, "table_open", "table_close")
            start, stop = tok.map if tok.map else (0, 0)
            rows = [ln.strip() for ln in lines[start:stop]]
            ncols = max((len(_split_gfm_cells(r)) for r in rows), default=0)
            blocks.append({
                "kind": "table",
                "rows": rows,
                "aligns": _parse_table_aligns(rows[1] if len(rows) > 1 else "", ncols),
            })
            i = close + 1

        elif t == "html_block":
            raw = tok.content.strip()
            img = _HTML_IMG_RE.fullmatch(raw)
            if img:
                src = _html_attr(img.group(0), "src")
                if src:
                    blocks.append(_image_block(src, _html_attr(img.group(0), "alt")))
            else:
                # 容器标签（<div> / <figure> 等）无法在 docx/pdf 中还原，
                # 去掉标签保留文字，避免整段内容消失
                text = _ANY_TAG_RE.sub("", raw).strip()
                if text:
                    blocks.append({"kind": "para", "text": text})
            i += 1

        elif t == "footnote_block_open":
            close = _match_close(tokens, i, "footnote_block_open",
                                 "footnote_block_close")
            j = i + 1
            while j < close:
                if tokens[j].type == "footnote_open":
                    label = (tokens[j].meta or {}).get("label", "")
                    k = j + 1
                    parts: list[str] = []
                    while k < close and tokens[k].type != "footnote_close":
                        if tokens[k].type == "inline":
                            parts.append(tokens[k].content)
                        k += 1
                    body = " ".join(p.strip() for p in parts if p.strip())
                    blocks.append({"kind": "para", "text": f"[^{label}] {body}"})
                    j = k + 1
                else:
                    j += 1
            i = close + 1

        else:
            i += 1

    return blocks, i


def parse_markdown(text: str) -> list[dict]:
    """把 Markdown 文本解析为块级结构列表。

    每个块为 dict，kind ∈ heading/para/code/math/image/quote/list/table/hr。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    tokens = _MD.parse(text)
    lines = text.split("\n")
    blocks, _ = _parse_blocks(tokens, 0, len(tokens), lines)
    return blocks
