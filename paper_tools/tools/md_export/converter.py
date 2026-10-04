"""通用 Markdown → Word (.docx) / PDF 转换核心实现。

适用于任意 Markdown 文档（技术笔记、博客、翻译稿、论文等），
对公式与网络图片做了重点处理：

* 图片处理
  - 本地相对/绝对路径直接解析；
  - `http(s)://` 网络图片按 URL 哈希缓存下载到 `<stem>.md_export/images/`
    （自动带浏览器伪装头、代理与重试，404 负缓存避免重复请求），仅首次联网；
  - SVG 先尝试同名位图，否则栅格化为 PNG；
  - webp / tiff 等格式在写入 DOCX 前用 Pillow 转为 PNG（python-docx 不支持）。

* 公式处理
  - 支持 $...$、$$...$$、\\(...\\)、\\[...\\]、\\begin{equation} 等环境；
  - 用 matplotlib mathtext 渲染为透明 PNG：
    inline 公式随文嵌入（基线高度对齐正文），display 公式单独成行居中；
  - 代码块 / 行内代码内的 $ 与 \\ 命令不会被误判为公式。

* 图表（diagram-as-code）处理
  - 主流 Markdown 编辑器（Typora 等）会原生渲染的三种围栏：```mermaid、
    ```flow（flowchart.js）、```sequence（js-sequence-diagrams），
    统一渲染为 PNG 后居中嵌入（否则会在文档里显示源码）；
  - flow / sequence 会先机械转译为 Mermaid 再渲染（无可用在线服务）；
  - 后端可在 本地 mermaid-cli / 在线 mermaid.ink 之间选择（见 config 的
    PAPER_TOOLS_MD_MERMAID），渲染失败则回退为普通代码块，不中断转换。

支持的语法：标题、段落（含行尾两空格硬换行）、有序/无序列表（两级嵌套）、
任务列表 - [ ] / - [x]、GFM 表格（含列对齐 :--- / :--: / ---:）、引用块、
围栏/缩进代码块、分隔线、行内样式（粗体/斜体/行内代码/链接/删除线）、
HTML <img> 与 <br>、引用式链接 [text][ref]。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from ...config import get_settings
from ...core.exporter import (
    _CN_FONT_CANDIDATES,
    _MONO_FONT_CANDIDATES,
    _find_font,
    _parse_table_rows,
    _split_gfm_cells,
    _strip_inline,
)
from ...core.user_io import confirm_overwrite
from ...core.math_render import render_math_to_png
from ...core.diagram_render import diagram_kind, render_diagram_to_png
from ...logging_setup import get_logger

logger = get_logger()

# ──────────────────────────────────────────────
# 行内片段（inline segment）模型与解析
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


# 行内 token 正则（顺序即优先级；先匹配的起始位置优先）
_TOKEN_RE = re.compile(
    r"(?P<img>!\[(?P<img_alt>[^\]]*)\]\(\s*<?(?P<img_src>[^)\s]+)>?(?:\s+\"[^\"]*\")?\s*\))"
    r"|(?P<himg><img\b[^>]*?>)"
    r"|(?P<math_dd>\$\$(?!\s)(?P<mdd>[^$]+?)\$\$)"
    r"|(?P<math_p>\\\((?P<mp>.+?)\\\))"
    r"|(?P<math_b>\\\[(?P<mb>.+?)\\\])"
    r"|(?P<math_i>\$(?!\s)(?P<mi>(?:[^$\n\\]|\\.)+?)\$(?!\d))"
    r"|(?P<code>`(?P<code_t>[^`\n]+)`)"
    r"|(?P<bold>\*\*(?P<bold_t>[^*\n]+?)\*\*|__(?P<bold_u>[^_\n]+?)__)"
    r"|(?P<strike>~~(?P<strike_t>.+?)~~)"
    r"|(?P<italic>\*(?P<it_t>[^*\n]+?)\*|(?<![0-9A-Za-z])_(?P<it_u>[^_\n]+?)_(?![0-9A-Za-z]))"
    r"|(?P<link>\[(?P<link_t>[^\]]*)\]\((?P<link_h>[^)\s]+)\))"
    r"|(?P<brtag><br\s*/?>)",
    re.I,
)

# 残留的简单 HTML 标签清理（保留标签内文本）
_HTML_TAG_RE = re.compile(
    r"</?(?:b|i|em|strong|sub|sup|u|s|span|div|p|font|small|center|mark|kbd|code|a)\b[^>]*>",
    re.I,
)

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _pdf_code_font(pdf, text: str) -> str:
    """PDF 代码字体选择：等宽字体缺少 CJK 字形时回退中文字体。"""
    mono = getattr(pdf, "_has_mono", False)
    if mono and not _CJK_RE.search(text):
        return "CNMono"
    return "CNBody"


# PDF 嵌入字体（宋体等 GBK 字体）缺少的符号 → GBK 内等价字形，
# 避免 fpdf2 输出 "missing glyphs" 警告并显示为空白。
_PDF_SYMBOL_MAP = str.maketrans({
    "\u2713": "\u221a",  # ✓ → √
    "\u2714": "\u221a",  # ✔ → √
    "\u2717": "\u00d7",  # ✗ → ×
    "\u2718": "\u00d7",  # ✘ → ×
    "\u2611": "\u221a",  # ☑ → √
    "\u2612": "\u00d7",  # ☒ → ×
    "\u2610": "\u25cb",  # ☐ → ○
    "\u279c": "\u2192", "\u27a1": "\u2192",  # ➜ ➡ → →
    "\u2605": "\u2605", "\u2606": "\u2606",  # ★☆ GBK 已有，保持
})


def _pdf_safe_text(text: str) -> str:
    """PDF 写入前的字形安全替换。"""
    return text.translate(_PDF_SYMBOL_MAP)


def _clean_text(t: str) -> str:
    return _HTML_TAG_RE.sub("", t)


def _parse_inline(text: str, bold: bool = False, italic: bool = False,
                  strike: bool = False, href: str = "") -> list[Seg]:
    """把一行 Markdown 文本解析为行内片段列表（支持嵌套样式）。"""
    segs: list[Seg] = []
    pos = 0
    for m in _TOKEN_RE.finditer(text):
        if m.start() > pos:
            segs.append(Seg("text", _clean_text(text[pos:m.start()]),
                            bold, italic, strike, href))
        pos = m.end()
        g = m.groupdict()
        if g["img"] is not None:
            segs.append(Seg("img", g["img_src"], bold, italic, strike,
                            alt=g["img_alt"] or ""))
        elif g["himg"] is not None:
            tag = g["himg"]
            sm = re.search(r"src\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", tag, re.I)
            am = re.search(r"alt\s*=\s*(\"[^\"]*\"|'[^']*')", tag, re.I)
            src = sm.group(1).strip("\"'") if sm else ""
            alt = am.group(1).strip("\"'") if am else ""
            if src:
                segs.append(Seg("img", src, bold, italic, strike, alt=alt))
        elif g["math_dd"] is not None:
            segs.append(Seg("math", g["mdd"].strip(), bold, italic, strike,
                            display=True))
        elif g["math_p"] is not None:
            segs.append(Seg("math", g["mp"].strip(), bold, italic, strike))
        elif g["math_b"] is not None:
            segs.append(Seg("math", g["mb"].strip(), bold, italic, strike))
        elif g["math_i"] is not None:
            segs.append(Seg("math", g["mi"].strip(), bold, italic, strike))
        elif g["code"] is not None:
            segs.append(Seg("code", g["code_t"], bold, italic, strike))
        elif g["bold"] is not None:
            inner = g["bold_t"] if g["bold_t"] is not None else g["bold_u"]
            segs.extend(_parse_inline(inner, True, italic, strike, href))
        elif g["strike"] is not None:
            segs.extend(_parse_inline(g["strike_t"], bold, italic, True, href))
        elif g["italic"] is not None:
            inner = g["it_t"] if g["it_t"] is not None else g["it_u"]
            segs.extend(_parse_inline(inner, bold, True, strike, href))
        elif g["link"] is not None:
            segs.extend(_parse_inline(g["link_t"], bold, italic, strike,
                                      g["link_h"]))
        elif g["brtag"] is not None:
            segs.append(Seg("br"))
    if pos < len(text):
        segs.append(Seg("text", _clean_text(text[pos:]), bold, italic, strike, href))
    return segs


# ──────────────────────────────────────────────
# 块级结构解析
# ──────────────────────────────────────────────

_FENCE_RE = re.compile(r"^ {0,3}(```+|~~~+)\s*([A-Za-z0-9_+\-]*)\s*$")
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_HR_RE = re.compile(r"^ {0,3}([-*_])\s*(?:\1\s*){2,}$")
_QUOTE_RE = re.compile(r"^ {0,3}>")
_TABLE_SEP_RE = re.compile(r"^ {0,3}\|?[\s:\-|]+\|?\s*$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_IMG_LINE_RE = re.compile(r"^ {0,3}!\[[^\]]*\]\([^)]+\)\s*$")
_HTML_IMG_LINE_RE = re.compile(r"^ {0,3}<img\b", re.I)
_REF_DEF_RE = re.compile(r"^ {0,3}\[([^\]]+)\]:\s*(\S+)")
_REF_USE_RE = re.compile(r"(!?)\[([^\]]+)\]\[([^\]]*)\]")
_ENV_BEGIN_RE = re.compile(
    r"^ {0,3}\\begin\{(equation\*?|align\*?|gather\*?|eqnarray\*?|multline\*?)\}")
_DISPLAY_DOLLAR_RE = re.compile(r"^ {0,3}\$\$")
_HTML_BLOCK_RE = re.compile(r"^ {0,3}<(?:div|table|figure|details|sup|sub)\b", re.I)


def _apply_refs(text: str, refmap: dict[str, str]) -> str:
    """把引用式链接/图片 [text][ref] 展开为 [text](url)。"""
    def _sub(m: re.Match) -> str:
        bang, label, ref = m.group(1), m.group(2), m.group(3)
        key = (ref.strip() or label).lower()
        url = refmap.get(key)
        if url:
            return f"{bang}[{label}]({url})"
        return m.group(0)
    return _REF_USE_RE.sub(_sub, text)


def _is_block_start(line: str) -> bool:
    s = line.lstrip(" ")
    return bool(
        _FENCE_RE.match(line) or _HEADING_RE.match(line) or _HR_RE.match(line)
        or _QUOTE_RE.match(line) or _DISPLAY_DOLLAR_RE.match(line)
        or _ENV_BEGIN_RE.match(line) or _LIST_RE.match(line)
        or _IMG_LINE_RE.match(line) or _HTML_IMG_LINE_RE.match(line)
        or s.startswith("\\[") or s.startswith("|")
    )


def _join_paragraph(parts: list[tuple[str, bool]]) -> str:
    """段落合并：CJK 相邻不加空格，否则加空格；上一行行尾两空格 → <br>。"""
    out = ""
    for idx, (ln, prev_hard) in enumerate(parts):
        if idx == 0:
            out = ln
            continue
        if parts[idx - 1][1]:  # 上一行以两个空格结尾 = 硬换行
            out += "<br>" + ln
        elif _CJK_RE.search(out[-1]) and _CJK_RE.search(ln[0]):
            out += ln
        else:
            out += " " + ln
    return out


_TASK_RE = re.compile(r"^\[([ xX])\]\s+(.*)$")


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


def parse_markdown(text: str) -> list[dict]:
    """把 Markdown 文本解析为块级结构列表。

    每个块为 dict，kind ∈ heading/para/code/math/image/quote/list/table/hr。
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    # 预扫描引用式链接定义 [ref]: url
    refmap: dict[str, str] = {}
    kept: list[str] = []
    for ln in lines:
        m = _REF_DEF_RE.match(ln)
        if m:
            refmap[m.group(1).strip().lower()] = m.group(2)
        else:
            kept.append(ln)
    lines = kept

    blocks: list[dict] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue

        # 围栏代码块
        m = _FENCE_RE.match(line)
        if m:
            fence, lang = m.group(1), m.group(2)
            close_re = re.compile(
                r"^ {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*$")
            i += 1
            buf: list[str] = []
            while i < n and not close_re.match(lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1  # 跳过结束围栏（或 EOF）
            blocks.append({"kind": "code", "text": "\n".join(buf), "lang": lang})
            continue

        # display 公式 $$ ... $$
        if _DISPLAY_DOLLAR_RE.match(line):
            if len(stripped) > 4 and stripped.endswith("$$"):
                blocks.append({"kind": "math", "text": stripped.strip("$").strip()})
                i += 1
                continue
            buf = []
            i += 1
            while i < n and not lines[i].strip().endswith("$$"):
                buf.append(lines[i].strip())
                i += 1
            if i < n:
                buf.append(lines[i].strip()[:-2].strip())
                i += 1
            blocks.append({"kind": "math", "text": "\n".join(buf).strip()})
            continue

        # display 公式 \[ ... \]
        if stripped.startswith("\\["):
            buf = [stripped]
            if not stripped.endswith("\\]"):
                i += 1
                while i < n and not lines[i].rstrip().endswith("\\]"):
                    buf.append(lines[i].strip())
                    i += 1
                if i < n:
                    buf.append(lines[i].strip())
                    i += 1
            else:
                i += 1
            t = "\n".join(buf).strip()
            t = t.removeprefix("\\[").removesuffix("\\]").strip()
            blocks.append({"kind": "math", "text": t})
            continue

        # display 公式 \begin{equation} ... \end{equation}
        m = _ENV_BEGIN_RE.match(line)
        if m:
            env = m.group(1)
            buf = [stripped]
            i += 1
            while i < n and f"\\end{{{env}}}" not in lines[i]:
                buf.append(lines[i].strip())
                i += 1
            if i < n:
                buf.append(lines[i].strip())
                i += 1
            blocks.append({"kind": "math", "text": "\n".join(buf).strip()})
            continue

        # 标题
        m = _HEADING_RE.match(line)
        if m:
            blocks.append({"kind": "heading", "level": len(m.group(1)),
                           "text": _apply_refs(m.group(2), refmap)})
            i += 1
            continue

        # 分隔线
        if _HR_RE.match(line):
            blocks.append({"kind": "hr"})
            i += 1
            continue

        # 引用块
        if _QUOTE_RE.match(line):
            buf = []
            while i < n and (_QUOTE_RE.match(lines[i]) or
                             (lines[i].strip() and buf and not _is_block_start(lines[i]))):
                buf.append(re.sub(r"^ {0,3}>\s?", "", lines[i]))
                i += 1
            blocks.append({"kind": "quote", "children": parse_markdown("\n".join(buf))})
            continue

        # GFM 表格（首行 |...|，次行分隔行，支持列对齐）
        if stripped.startswith("|") and i + 1 < n \
                and _TABLE_SEP_RE.match(lines[i + 1].strip()) \
                and "-" in lines[i + 1]:
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            sep = rows[1] if len(rows) > 1 else ""
            ncols = max(len(_split_gfm_cells(r)) for r in rows)
            blocks.append({"kind": "table", "rows": rows,
                           "aligns": _parse_table_aligns(sep, ncols)})
            continue

        # 列表（支持两级嵌套、任务列表、单行续行）
        m = _LIST_RE.match(line)
        if m:
            items = []
            while i < n:
                mm = _LIST_RE.match(lines[i])
                if mm:
                    depth = min(2, len(mm.group(1)) // 2)
                    text = _apply_refs(mm.group(3), refmap)
                    task: bool | None = None
                    tm = _TASK_RE.match(text)
                    if tm:
                        task = tm.group(1).lower() == "x"
                        text = tm.group(2)
                    items.append({
                        "depth": depth,
                        "ordered": mm.group(2)[0].isdigit(),
                        "task": task,
                        "text": text,
                    })
                    i += 1
                elif (lines[i].strip() and items
                      and not _is_block_start(lines[i])
                      and len(lines[i]) - len(lines[i].lstrip(" ")) >= 2):
                    items[-1]["text"] += " " + lines[i].strip()
                    i += 1
                else:
                    break
            blocks.append({"kind": "list", "items": items})
            continue

        # 独立成行的图片 / HTML img
        if _IMG_LINE_RE.match(line) or _HTML_IMG_LINE_RE.match(line):
            for seg in _parse_inline(stripped):
                if seg.kind == "img":
                    blocks.append({"kind": "image", "src": seg.text, "alt": seg.alt})
            i += 1
            continue

        # HTML 块级标签：跳过标签行本身（内容按普通文本继续处理）
        if _HTML_BLOCK_RE.match(line):
            i += 1
            continue

        # 段落：收集到空行或块级起始（行尾两空格 = 硬换行）
        para_parts: list[tuple[str, bool]] = [(stripped, line.endswith("  "))]
        i += 1
        while i < n and lines[i].strip() and not _is_block_start(lines[i]) \
                and not _HTML_BLOCK_RE.match(lines[i]):
            para_parts.append((lines[i].strip(), lines[i].endswith("  ")))
            i += 1
        blocks.append({"kind": "para",
                       "text": _apply_refs(_join_paragraph(para_parts), refmap)})

    return blocks


# ──────────────────────────────────────────────
# 图片与公式的本地化准备
# ──────────────────────────────────────────────

_DOCX_OK_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp"}


@dataclass
class ConvertCtx:
    """转换上下文：源目录、下载缓存目录、公式/图表渲染缓存目录。

    missed_urls: 本次运行中确认「4xx 不存在」的 URL 集合（会话级负缓存），
                 避免 docx/pdf 两轮渲染重复请求。
    """
    source_dir: Path
    images_dir: Path
    math_dir: Path
    diagram_dir: Path = field(default_factory=Path)
    settings: object = field(default_factory=get_settings)
    missed_urls: set = field(default_factory=set)

    @property
    def font(self) -> str:
        """DOCX/PDF 中文正文字体（可通过 PAPER_TOOLS_MD_FONT 配置）。"""
        name = str(getattr(self.settings, "md_export_font", "") or "").strip()
        return name or "宋体"


def _download_image(url: str, dest: Path, ctx: ConvertCtx) -> str:
    """下载图片到 dest。

    返回 "ok" / "miss:<status>"（4xx 永久不存在，不重试）/ "error:<原因>"
    （网络异常或 5xx，按配置指数退避重试）。
    """
    import time

    import requests

    cfg = ctx.settings
    proxy = (getattr(cfg, "download_proxy", "") or "").strip()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    retries = int(getattr(cfg, "download_max_retries", 3) or 0)
    last: Exception | str = ""
    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                url, timeout=getattr(cfg, "download_timeout", 60),
                headers=getattr(cfg, "download_headers", None),
                stream=True, proxies=proxies)
            if resp.status_code == 200:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                return "ok"
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                return f"miss:{resp.status_code}"
            last = f"HTTP {resp.status_code}"
        except Exception as e:  # noqa: BLE001
            last = e
        if attempt < retries:
            backoff = min(2 ** attempt, 30)
            logger.warning(f"  下载异常 ({last})，第 {attempt + 1}/{retries + 1}"
                           f" 次尝试后重试 {url}（{backoff}s）")
            time.sleep(backoff)
    return f"error:{last}"


def _resolve_image(src: str, ctx: ConvertCtx, *,
                   rasterize_svg: bool = True) -> Path | None:
    """把图片 src 解析为本地文件；网络图片按 URL 哈希缓存下载。

    带负缓存：确认 4xx（资源不存在）的 URL 记入 ctx.missed_urls 并在缓存
    目录写 .miss 标记文件（跨运行持久），后续解析直接跳过，不再发起请求。
    网络类失败只做会话级缓存（不写标记），下次运行会重试。

    rasterize_svg=False 时保留 SVG 原格式（仅输出 HTML/markdown 时才需要，
    此时 SVG 能作为 data URI 直接渲染，可保住矢量清晰度）。
    """
    src = (src or "").strip()
    if src.startswith("<") and src.endswith(">"):
        src = src[1:-1]
    if not src:
        return None
    if src.startswith(("http://", "https://")):
        # SVG 嵌入兼容性差：arxiv 等站点通常有同名位图，优先尝试 .png/.jpg
        candidates = [src]
        if urlparse(src).path.lower().endswith(".svg"):
            base = src[:-4]
            candidates = [base + ".png", base + ".jpg", src]
        for cand in candidates:
            u = urlparse(cand)
            ext = Path(u.path).suffix.lower()
            if ext not in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
                           ".tif", ".tiff", ".svg"}:
                ext = ".png"
            key = hashlib.md5(cand.encode("utf-8")).hexdigest()[:14]
            dest = ctx.images_dir / f"net_{key}{ext}"
            if dest.exists() and dest.stat().st_size > 100:
                return _finalize_image(dest, ctx, rasterize_svg)
            miss_marker = dest.with_name(dest.name + ".miss")
            if cand in ctx.missed_urls or miss_marker.exists():
                continue
            logger.info(f"  下载网络图片: {cand}")
            status = _download_image(cand, dest, ctx)
            if status == "ok" and dest.exists() and dest.stat().st_size > 100:
                return _finalize_image(dest, ctx, rasterize_svg)
            if status.startswith("miss"):
                # 4xx = 资源永久不存在：持久化负缓存，之后不再请求
                ctx.missed_urls.add(cand)
                try:
                    miss_marker.touch()
                except Exception:  # noqa: BLE001
                    pass
                logger.warning(f"  图片不存在 ({status.split(':', 1)[1]} 不可达): {cand}")
            else:
                logger.warning(f"  下载失败 ({status.split(':', 1)[1]}): {cand}")
        return None
    p = Path(src)
    if not p.is_absolute():
        p = ctx.source_dir / p
    return _finalize_image(p, ctx, rasterize_svg) if p.exists() else None


def _rasterize_svg(svg_path: Path, ctx: ConvertCtx) -> Path | None:
    """把 SVG 栅格化为 PNG（依赖 svglib；未安装时提示并返回 None）。"""
    try:
        from svglib.svglib import svg2rlg  # noqa: F401
        from reportlab.graphics import renderPM
    except ImportError:
        logger.warning(
            "  SVG 图片需要 svglib 支持：运行 uv add svglib 后重试"
            "（本次将以占位文本代替该图）")
        return None
    key = hashlib.md5(str(svg_path.resolve()).encode("utf-8")).hexdigest()[:12]
    out = ctx.images_dir / f"svg_{svg_path.stem}_{key}.png"
    if out.exists() and out.stat().st_size > 100:
        return out
    # 目录只有在下过网络图片时才存在；纯本地图片的场景需自行创建，
    # 否则写入报 FileNotFoundError 并静默回退为原始 SVG。
    ctx.images_dir.mkdir(parents=True, exist_ok=True)
    try:
        drawing = svg2rlg(str(svg_path))
        if drawing is None:
            return None
        scale = 2.0  # 放大 2 倍栅格化，保证印刷清晰度
        drawing.width *= scale
        drawing.height *= scale
        drawing.scale(scale, scale)
        renderPM.drawToFile(drawing, str(out), fmt="PNG", dpi=150)
        if out.exists() and out.stat().st_size > 100:
            return out
    except Exception as e:
        logger.warning(f"  SVG 栅格化失败 ({svg_path.name}): {e}")
    return None


def _finalize_image(path: Path, ctx: ConvertCtx,
                    rasterize_svg: bool = True) -> Path:
    """图片落地后的统一处理：SVG 栅格化为位图，便于 docx/pdf 嵌入。

    docx/pdf 无法嵌入 SVG，必须栅格化；输出 HTML/markdown 时可传
    rasterize_svg=False 保留矢量（栅格化失败时同样回退为原 SVG）。
    """
    if rasterize_svg and path.suffix.lower() == ".svg":
        png = _rasterize_svg(path, ctx)
        return png or path
    return path


def _docx_safe_image(path: Path, ctx: ConvertCtx) -> Path | None:
    """DOCX 仅支持 png/jpg/gif/bmp；webp/tiff 等先用 Pillow 转 PNG。"""
    if path.suffix.lower() in _DOCX_OK_IMG_EXTS:
        return path
    try:
        from PIL import Image
        key = hashlib.md5(str(path).encode("utf-8")).hexdigest()[:10]
        out = ctx.images_dir / f"conv_{path.stem}_{key}.png"
        if out.exists():
            return out
        # 同 _rasterize_svg：纯本地图片时 images_dir 可能尚未创建
        ctx.images_dir.mkdir(parents=True, exist_ok=True)
        with Image.open(path) as im:
            im.convert("RGBA" if "A" in im.mode or im.mode == "P" else "RGB") \
              .save(out, "PNG")
        return out
    except Exception as e:
        logger.warning(f"  图片格式转换失败 ({path.suffix}): {e}")
        return None


def _image_size(path: Path) -> tuple[int, int, float]:
    """返回 (宽px, 高px, dpi)；失败返回 (0,0,96)。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            dpi = im.info.get("dpi", (96, 96))
            d = float(dpi[0]) if dpi and dpi[0] else 96.0
            if not (10 < d < 1200):
                d = 96.0
            return im.size[0], im.size[1], d
    except Exception:
        return 0, 0, 96.0


def _fit_cm(w_px: int, h_px: int, dpi: float,
            max_w_cm: float, max_h_cm: float) -> tuple[float, float]:
    """像素尺寸 → cm 并等比缩放至 (max_w, max_h) 内。"""
    w_cm = w_px / dpi * 2.54
    h_cm = h_px / dpi * 2.54
    if w_cm <= 0 or h_cm <= 0:
        return min(6.0, max_w_cm), min(4.0, max_h_cm)
    scale = min(1.0, max_w_cm / w_cm, max_h_cm / h_cm)
    return w_cm * scale, h_cm * scale


def _math_to_text(tex: str) -> str:
    """表格 cell 中公式的纯文本退化（unicode 近似）。"""
    s = " ".join(tex.split())
    s = re.sub(r"\\mathcal\{([A-Za-z])\}", r"\1", s)
    s = re.sub(r"\\(mathbb|mathbf|mathrm|mathit|mathfrak|text|texttt|textit|textrm)\{([^{}]*)\}",
               r"\2", s)
    s = re.sub(r"[_^]\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\begin\{[a-zA-Z*]+\}|\\end\{[a-zA-Z*]+\}", " ", s)
    for a, b in ((r"\geq", "≥"), (r"\leq", "≤"), (r"\approx", "≈"),
                 (r"\times", "×"), (r"\cdot", "·"), (r"\infty", "∞"),
                 (r"\alpha", "α"), (r"\beta", "β"), (r"\gamma", "γ"),
                 (r"\delta", "δ"), (r"\lambda", "λ"), (r"\mu", "μ"),
                 (r"\sigma", "σ"), (r"\sum", "Σ"), (r"\int", "∫"),
                 (r"\rightarrow", "→"), (r"\leftarrow", "←"), (r"\sim", "~")):
        s = s.replace(a, b)
    s = re.sub(r"\\[a-zA-Z]+", " ", s)
    s = re.sub(r"[{}]", "", s)
    return s.strip() or tex.strip()


# ──────────────────────────────────────────────
# DOCX 渲染
# ──────────────────────────────────────────────

def _set_eastasia(run, name: str = "宋体") -> None:
    """给 run 设置中文字体（否则中文标题/正文在 Word 中可能回退错误字体）。"""
    try:
        from docx.oxml.ns import qn
        rpr = run._element.get_or_add_rPr()
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is None:
            from lxml import etree
            rfonts = etree.SubElement(rpr, qn("w:rFonts"))
        rfonts.set(qn("w:eastAsia"), name)
    except Exception:
        pass


def _docx_insert_math(p, tex: str, ctx: ConvertCtx) -> None:
    """在段落中插入公式：渲染为 PNG（inline 随文，0.55cm 基线高）。"""
    from docx.shared import Cm, Pt, RGBColor

    png = render_math_to_png(tex, display=False, out_dir=ctx.math_dir)
    if png is None:
        run = p.add_run(f" {tex} ")
        run.italic = True
        run.font.size = Pt(10)
        run.font.color.rgb = RGBColor(90, 90, 90)
        return
    from PIL import Image as PILImage
    with PILImage.open(png) as im:
        iw, ih = im.size
    if iw > 0 and ih > 0:
        h_cm = 0.55
        w_cm = h_cm * (iw / ih)
        if w_cm > 8.0:  # 超长 inline 公式限宽
            w_cm = 8.0
            h_cm = w_cm * (ih / iw)
        p.add_run().add_picture(str(png), height=Cm(h_cm))
    else:
        p.add_run(f" {tex} ")


def _docx_insert_image(p, src: str, ctx: ConvertCtx, *,
                       max_w_cm: float = 8.0, alt: str = "") -> None:
    from docx.shared import Cm, Pt, RGBColor

    path = _resolve_image(src, ctx)
    if path is None:
        run = p.add_run(f"[图片缺失: {src}]")
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(150, 150, 150)
        return
    safe = _docx_safe_image(path, ctx)
    if safe is None:
        run = p.add_run(f"[图片: {src}]")
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(150, 150, 150)
        return
    w_px, h_px, dpi = _image_size(safe)
    w_cm, h_cm = _fit_cm(w_px, h_px, dpi, max_w_cm, 21.0)
    p.add_run().add_picture(str(safe), width=Cm(w_cm))


def _docx_insert_diagram_block(doc, kind: str, code: str,
                               ctx: ConvertCtx) -> bool:
    """把图表代码块渲染为图片并居中写入；失败返回 False（调用方回退代码块）。"""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm

    png = render_diagram_to_png(kind, code, ctx.diagram_dir, ctx.settings)
    if png is None:
        return False
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    w_px, h_px, dpi = _image_size(png)
    w_cm, _ = _fit_cm(w_px, h_px, dpi, 16.0, 22.0)
    p.add_run().add_picture(str(png), width=Cm(w_cm))
    return True


def _docx_write_segs(p, segs: list[Seg], ctx: ConvertCtx) -> None:
    from docx.shared import Pt, RGBColor

    for seg in segs:
        if seg.kind == "text":
            if not seg.text:
                continue
            run = p.add_run(seg.text)
            run.bold = seg.bold
            run.italic = seg.italic
            if seg.strike:
                run.font.strike = True
            if seg.href:
                run.font.color.rgb = RGBColor(0, 0, 200)
                run.underline = True
            _set_eastasia(run)
        elif seg.kind == "code":
            run = p.add_run(seg.text)
            run.font.name = "Consolas"
            run.font.size = Pt(10)
            _set_eastasia(run, ctx.font)
        elif seg.kind == "br":
            p.add_run().add_break()
        elif seg.kind == "math":
            _docx_insert_math(p, seg.text, ctx)
        elif seg.kind == "img":
            _docx_insert_image(p, seg.text, ctx, alt=seg.alt)


_HEADING_SIZES = {1: 22, 2: 18, 3: 15, 4: 13, 5: 12, 6: 11}


def _docx_render_blocks(doc, blocks: list[dict], ctx: ConvertCtx,
                        quote: bool = False) -> None:
    """按顺序把块级结构写入 docx。quote=True 时套用引用样式。"""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt, RGBColor

    for b in blocks:
        kind = b["kind"]
        if kind == "heading":
            level = b["level"]
            p = doc.add_heading("", level=level)
            _docx_write_segs(p, _parse_inline(b["text"]), ctx)
            for run in p.runs:
                run.font.size = Pt(_HEADING_SIZES.get(level, 12))
                run.font.color.rgb = RGBColor(0, 0, 0)
                _set_eastasia(run, ctx.font)
            continue

        if kind == "para":
            p = doc.add_paragraph()
            _docx_write_segs(p, _parse_inline(b["text"]), ctx)
            if quote:
                p.paragraph_format.left_indent = Cm(0.8)
                for run in p.runs:
                    run.font.color.rgb = RGBColor(110, 110, 110)
                    run.font.size = Pt(10.5)
            continue

        if kind == "math":
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            tex = b["text"]
            png = render_math_to_png(tex, display=True, out_dir=ctx.math_dir)
            if png is not None:
                from PIL import Image as PILImage
                with PILImage.open(png) as im:
                    iw, ih = im.size
                w_cm = iw / 200 * 2.54  # math_render 输出 DPI=200
                h_cm = ih / 200 * 2.54
                if w_cm > 16.0:
                    w_cm, h_cm = 16.0, 16.0 * ih / iw
                if h_cm > 20.0:
                    h_cm, w_cm = 20.0, 20.0 * iw / ih
                p.add_run().add_picture(str(png), width=Cm(w_cm))
            else:
                run = p.add_run(tex)
                run.italic = True
                run.font.size = Pt(11)
                run.font.color.rgb = RGBColor(90, 90, 90)
                _set_eastasia(run)
            continue

        if kind == "image":
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _docx_insert_image(p, b["src"], ctx, max_w_cm=16.0, alt=b["alt"])
            alt = (b["alt"] or "").strip()
            # alt 是有效描述（非纯文件名）时作为图注
            if alt and Path(alt).suffix == "" and alt != b["src"]:
                cap = doc.add_paragraph()
                cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
                run = cap.add_run(alt)
                run.font.size = Pt(9)
                run.font.color.rgb = RGBColor(110, 110, 110)
                run.italic = True
                _set_eastasia(run, ctx.font)
            continue

        if kind == "code":
            code = b["text"]
            if not code:
                continue
            kind_name = diagram_kind(b.get("lang", ""))
            if kind_name and _docx_insert_diagram_block(doc, kind_name, code, ctx):
                continue
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.5)
            lines = code.split("\n")
            for idx, ln in enumerate(lines):
                run = p.add_run(ln)
                run.font.name = "Consolas"
                run.font.size = Pt(9)
                run.font.color.rgb = RGBColor(60, 60, 60)
                _set_eastasia(run, ctx.font)
                if idx < len(lines) - 1:
                    run.add_break()
            continue

        if kind == "list":
            ordered_n = 0  # 连续有序项计数（遇无序项复位）
            for item in b["items"]:
                depth = item["depth"]
                task = item.get("task")
                if task is not None:
                    # 任务列表项：复选框符号替代项目符号
                    ordered_n = 0
                    p = doc.add_paragraph()
                    p.paragraph_format.left_indent = Cm(0.8 + 0.8 * depth)
                    mk = p.add_run("✓ " if task else "□ ")
                    mk.bold = task
                elif item["ordered"]:
                    ordered_n += 1
                    marker = f"{ordered_n}. "
                    p = doc.add_paragraph()
                    p.paragraph_format.left_indent = Cm(0.8 + 0.8 * depth)
                    p.add_run(marker)
                else:
                    ordered_n = 0
                    style = {0: "List Bullet", 1: "List Bullet 2", 2: "List Bullet 3"}[depth]
                    try:
                        p = doc.add_paragraph(style=style)
                    except KeyError:
                        p = doc.add_paragraph()
                        p.paragraph_format.left_indent = Cm(0.8 + 0.8 * depth)
                _docx_write_segs(p, _parse_inline(item["text"]), ctx)
                if quote:
                    for run in p.runs:
                        run.font.color.rgb = RGBColor(110, 110, 110)
            continue

        if kind == "table":
            rows = _parse_table_rows(b["rows"])
            if not rows:
                continue
            ncols = max(len(r) for r in rows)
            aligns = b.get("aligns") or ["left"] * ncols
            _ALIGNS = {"left": WD_ALIGN_PARAGRAPH.LEFT,
                       "center": WD_ALIGN_PARAGRAPH.CENTER,
                       "right": WD_ALIGN_PARAGRAPH.RIGHT}
            table = doc.add_table(rows=len(rows), cols=ncols, style="Table Grid")
            for ri, row in enumerate(rows):
                for ci in range(ncols):
                    cell_text = row[ci] if ci < len(row) else ""
                    cell = table.cell(ri, ci)
                    cell.paragraphs[0].alignment = _ALIGNS.get(
                        aligns[ci] if ci < len(aligns) else "left",
                        WD_ALIGN_PARAGRAPH.LEFT)
                    cell.paragraphs[0].paragraph_format.space_before = Pt(2)
                    cell.paragraphs[0].paragraph_format.space_after = Pt(2)
                    _docx_write_segs(cell.paragraphs[0],
                                     _parse_inline(cell_text), ctx)
                    for run in cell.paragraphs[0].runs:
                        run.font.size = Pt(10)
                        if ri == 0:
                            run.bold = True
            doc.add_paragraph().paragraph_format.space_after = Pt(2)
            continue

        if kind == "quote":
            _docx_render_blocks(doc, b["children"], ctx, quote=True)
            continue

        if kind == "hr":
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run("─" * 40)
            run.font.color.rgb = RGBColor(180, 180, 180)
            continue


def blocks_to_docx(blocks: list[dict], out_path: Path, ctx: ConvertCtx) -> Path:
    """块级结构 → .docx。"""
    from docx import Document
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt

    doc = Document()
    section = doc.sections[0]
    section.page_width = Cm(21)
    section.page_height = Cm(29.7)
    for m in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(section, m, Cm(2.5))

    style = doc.styles["Normal"]
    style.font.name = ctx.font
    style.font.size = Pt(11)
    style.paragraph_format.line_spacing = 1.4
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        from lxml import etree
        rfonts = etree.SubElement(rpr, qn("w:rFonts"))
    rfonts.set(qn("w:eastAsia"), ctx.font)

    _docx_render_blocks(doc, blocks, ctx)
    doc.save(str(out_path))
    logger.info(f"DOCX 已导出: {out_path}")
    return out_path


# ──────────────────────────────────────────────
# PDF 渲染（fpdf2）
# ──────────────────────────────────────────────

# 常见中文字体名 → 字体文件（Windows 字体目录）；未命中回退 exporter 候选表
_PDF_FONT_FILES: dict[str, list[str]] = {
    "宋体": ["simsun.ttc", "simsunb.ttf", "SimsunExtG.ttf"],
    "微软雅黑": ["msyh.ttc", "msyh.ttf"],
    "黑体": ["simhei.ttf"],
    "楷体": ["simkai.ttf", "STKAITI.TTF", "KaiTi.ttf"],
    "仿宋": ["simfang.ttf", "STFANGSO.TTF"],
    "等线": ["Deng.ttf"],
}
_WIN_FONT_DIRS = [r"C:\Windows\Fonts"]


def _find_cn_font(name: str) -> str | None:
    """按字体名查找本地字体文件；找不到返回 None。"""
    files = _PDF_FONT_FILES.get(name)
    if not files:
        return None
    from pathlib import Path as _P
    candidates = [str(_P(d) / f) for d in _WIN_FONT_DIRS for f in files]
    return _find_font(candidates)


def _new_pdf(settings=None) -> "FPDF":
    from fpdf import FPDF

    preferred = str(getattr(settings, "md_export_font", "") or "").strip()
    cn_font = _find_cn_font(preferred) or _find_font(_CN_FONT_CANDIDATES)
    if cn_font is None:
        raise FileNotFoundError(
            "无法找到中文字体文件，请确保系统已安装宋体/黑体/微软雅黑之一。\n"
            f"搜索路径: {_CN_FONT_CANDIDATES}")
    # 标题用黑体系（无则回退正文字体）
    hei_candidates = [str(Path(r"C:\Windows\Fonts") / f)
                      for f in ("simhei.ttf", "msyhbd.ttc", "msyh.ttc")]
    heading_font = _find_font(hei_candidates) or cn_font
    mono_font = _find_font(_MONO_FONT_CANDIDATES)
    has_mono = mono_font is not None

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.add_font("CNBody", "", cn_font, uni=True)
    pdf.add_font("CNBody", "B", heading_font, uni=True)
    pdf.add_font("CNBody", "I", cn_font, uni=True)
    pdf.add_font("CNBody", "BI", heading_font, uni=True)
    if mono_font:
        pdf.add_font("CNMono", "", mono_font, uni=True)
    pdf._has_mono = has_mono  # noqa: SLF001
    return pdf


def _pdf_place_image(pdf, path: Path, page_width: float, max_w_mm: float) -> None:
    """在当前流位置插入内容图片：自动缩放、居中、必要时翻页。"""
    from PIL import Image as PILImage

    try:
        with PILImage.open(path) as im:
            iw, ih = im.size
        if iw <= 0 or ih <= 0:
            return
        _, _, dpi = _image_size(path)
        w_mm = iw / dpi * 25.4
        h_mm = ih / dpi * 25.4
        scale = min(1.0, max_w_mm / w_mm, 220.0 / h_mm)
        w_mm, h_mm = w_mm * scale, h_mm * scale
        if pdf.get_y() + h_mm + 4 > pdf.h - pdf.b_margin:
            pdf.add_page()
        x = pdf.l_margin + max(0.0, (page_width - w_mm) / 2)
        pdf.ln(2)
        pdf.image(str(path), x=x, y=pdf.get_y(), w=w_mm)
        pdf.set_y(pdf.get_y() + h_mm + 2)
        pdf.set_x(pdf.l_margin)
    except Exception as e:
        logger.warning(f"  PDF 图片嵌入失败: {path.name} ({e})")


def _pdf_insert_diagram_block(pdf, kind: str, code: str, page_width: float,
                              ctx: ConvertCtx) -> bool:
    """把图表代码块渲染为图片嵌入；失败返回 False（调用方回退代码块）。"""
    png = render_diagram_to_png(kind, code, ctx.diagram_dir, ctx.settings)
    if png is None:
        return False
    _pdf_place_image(pdf, png, page_width, max_w_mm=page_width)
    return True


def _pdf_write_segs(pdf, segs: list[Seg], page_width: float, ctx: ConvertCtx,
                    indent: float = 0.0, base_color=None) -> None:
    """把行内片段写入 PDF 流（公式/图片就地嵌入，文本流式换行）。"""
    for seg in segs:
        if seg.kind == "text":
            if not seg.text:
                continue
            style = ("B" if seg.bold else "") + ("I" if seg.italic else "")
            pdf.set_font("CNBody", style or "", 11)
            if seg.href:
                pdf.set_text_color(0, 0, 200)
            elif seg.strike:
                pdf.set_text_color(130, 130, 130)
            elif base_color:
                pdf.set_text_color(*base_color)
            else:
                pdf.set_text_color(0, 0, 0)
            pdf.write(6, _pdf_safe_text(seg.text))
        elif seg.kind == "code":
            pdf.set_font(_pdf_code_font(pdf, seg.text), "", 10)
            pdf.set_text_color(70, 70, 70)
            pdf.write(6, _pdf_safe_text(seg.text))
        elif seg.kind == "br":
            pdf.ln(6)
            if indent:
                pdf.set_x(pdf.l_margin + indent)
        elif seg.kind == "math":
            from ...core.exporter import _pdf_insert_math

            png = render_math_to_png(seg.text, display=seg.display,
                                     out_dir=ctx.math_dir)
            if png is not None:
                if seg.display:
                    pdf.set_text_color(0, 0, 0)
                    _pdf_insert_math(pdf, png, display=True,
                                     page_width=page_width, indent=indent)
                else:
                    _pdf_insert_math(pdf, png, display=False,
                                     page_width=page_width, indent=indent)
            else:
                pdf.set_font("CNBody", "I", 11)
                pdf.set_text_color(90, 90, 90)
                pdf.write(6, f" {seg.text} ")
        elif seg.kind == "img":
            path = _resolve_image(seg.text, ctx)
            if path is not None:
                pdf.set_text_color(0, 0, 0)
                _pdf_place_image(pdf, path, page_width,
                                 max_w_mm=min(page_width * 0.9, 120))
            else:
                pdf.set_font("CNBody", "I", 9)
                pdf.set_text_color(150, 150, 150)
                pdf.write(6, f"[图: {seg.text}]")
    pdf.set_text_color(0, 0, 0)


def _pdf_render_blocks(pdf, blocks: list[dict], page_width: float,
                       ctx: ConvertCtx, quote: bool = False) -> None:
    from fpdf.enums import XPos, YPos

    base_color = (110, 110, 110) if quote else None
    for b in blocks:
        kind = b["kind"]
        if kind == "heading":
            level = b["level"]
            sz = {1: 18, 2: 15, 3: 13, 4: 12}.get(level, 12)
            pdf.ln(3)
            pdf.set_font("CNBody", "B", sz)
            pdf.set_text_color(0, 0, 0)
            pdf.multi_cell(page_width, sz * 0.7,
                           _pdf_safe_text(_strip_inline(b["text"])),
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(2)
            continue

        if kind == "para":
            pdf.ln(1)
            if quote:
                pdf.set_x(pdf.l_margin + 6)
            _pdf_write_segs(pdf, _parse_inline(b["text"]), page_width, ctx,
                            indent=6 if quote else 0, base_color=base_color)
            pdf.ln(7)
            continue

        if kind == "math":
            tex = b["text"]
            png = render_math_to_png(tex, display=True, out_dir=ctx.math_dir)
            if png is not None:
                pdf.ln(2)
                from ...core.exporter import _pdf_insert_math
                _pdf_insert_math(pdf, png, display=True, page_width=page_width)
                pdf.ln(2)
            else:
                pdf.ln(2)
                pdf.set_font("CNBody", "I", 10)
                pdf.set_text_color(90, 90, 90)
                for part in tex.split("\n"):
                    pdf.multi_cell(page_width, 7, part, align="C",
                                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_text_color(0, 0, 0)
                pdf.ln(2)
            continue

        if kind == "image":
            path = _resolve_image(b["src"], ctx)
            if path is not None:
                _pdf_place_image(pdf, path, page_width, max_w_mm=page_width)
            else:
                pdf.set_font("CNBody", "I", 9)
                pdf.set_text_color(150, 150, 150)
                pdf.multi_cell(page_width, 6, f"[图片缺失: {b['src']}]",
                               new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_text_color(0, 0, 0)
            alt = (b["alt"] or "").strip()
            if alt and Path(alt).suffix == "" and alt != b["src"]:
                pdf.set_font("CNBody", "I", 9)
                pdf.set_text_color(110, 110, 110)
                pdf.multi_cell(page_width, 6, _pdf_safe_text(alt), align="C",
                               new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_text_color(0, 0, 0)
                pdf.ln(2)
            continue

        if kind == "code":
            code = b["text"]
            if not code:
                continue
            kind_name = diagram_kind(b.get("lang", ""))
            if kind_name and _pdf_insert_diagram_block(pdf, kind_name, code,
                                                       page_width, ctx):
                continue
            pdf.ln(2)
            pdf.set_font(_pdf_code_font(pdf, code), "", 9)
            pdf.set_text_color(60, 60, 60)
            pdf.multi_cell(page_width, 4.6, _pdf_safe_text(code),
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_text_color(0, 0, 0)
            pdf.ln(2)
            continue

        if kind == "list":
            ordered_n = 0  # 连续有序项计数（遇无序项复位）
            for item in b["items"]:
                depth = item["depth"]
                indent = 4 + 6 * depth
                task = item.get("task")
                if task is not None:
                    ordered_n = 0
                    marker = "✓ " if task else "□ "
                elif item["ordered"]:
                    ordered_n += 1
                    marker = f"{ordered_n}. "
                else:
                    ordered_n = 0
                    marker = "• "
                pdf.ln(1)
                pdf.set_x(pdf.l_margin + indent)
                pdf.set_font("CNBody", "", 11)
                pdf.set_text_color(*(base_color or (0, 0, 0)))
                pdf.write(6, _pdf_safe_text(marker))
                _pdf_write_segs(pdf, _parse_inline(item["text"]), page_width,
                                ctx, indent=indent, base_color=base_color)
                pdf.ln(6)
            continue

        if kind == "table":
            _pdf_table(pdf, b, page_width, ctx)
            continue

        if kind == "quote":
            pdf.ln(2)
            _pdf_render_blocks(pdf, b["children"], page_width, ctx, quote=True)
            continue

        if kind == "hr":
            pdf.ln(3)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
            pdf.ln(5)
            continue


def _pdf_table(pdf, b: dict, page_width: float, ctx: ConvertCtx) -> None:
    """GFM 表格 → fpdf2 table；纯公式 cell 以图片嵌入，其余为文本。"""
    rows = _parse_table_rows(b["rows"])
    if not rows:
        return
    ncols = max(len(r) for r in rows)
    col_w = page_width / ncols

    def _cell_content(text: str) -> tuple[str, Path | None]:
        segs = _parse_inline(text)
        # 纯公式 cell（去掉空白后只剩一个 math 片段）→ 图片
        nonempty = [s for s in segs if s.kind != "br" and
                    (s.kind != "text" or s.text.strip())]
        if len(nonempty) == 1 and nonempty[0].kind == "math":
            png = render_math_to_png(nonempty[0].text, display=False,
                                     out_dir=ctx.math_dir)
            if png is not None:
                return "", png
        plain = " ".join(
            _math_to_text(s.text) if s.kind == "math" else
            (s.text if s.kind in ("text", "code") else " ")
            for s in segs)
        return _strip_inline(plain).strip(), None

    aligns = b.get("aligns") or ["left"] * ncols
    _TA = {"left": "LEFT", "center": "CENTER", "right": "RIGHT"}
    text_align = tuple(_TA.get(a, "LEFT") for a in aligns)

    pdf.ln(2)
    with pdf.table(width=page_width, col_widths=tuple([col_w] * ncols),
                   text_align=text_align, line_height=5.5,
                   first_row_as_headings=True, borders_layout="ALL") as t:
        for row in rows:
            trow = t.row()
            for ci in range(ncols):
                cell_text = row[ci] if ci < len(row) else ""
                txt, png = _cell_content(cell_text)
                if png is not None:
                    trow.cell(img=str(png), img_fill_width=False,
                              align=text_align[ci] if ci < len(text_align) else "CENTER",
                              v_align="MIDDLE")
                else:
                    trow.cell(_pdf_safe_text(txt))
    pdf.ln(3)


def blocks_to_pdf(blocks: list[dict], out_path: Path, ctx: ConvertCtx) -> Path:
    """块级结构 → .pdf。"""
    pdf = _new_pdf(ctx.settings)
    page_width = pdf.w - pdf.l_margin - pdf.r_margin
    _pdf_render_blocks(pdf, blocks, page_width, ctx)
    pdf.output(str(out_path))
    logger.info(f"PDF 已导出: {out_path}")
    return out_path


# ──────────────────────────────────────────────
# 对外入口（与其他工具一致：run(...)）
# ──────────────────────────────────────────────

def run(md_path: str | Path | None = None, *, fmt: str | None = None,
        out_dir: str | Path | None = None,
        settings=None) -> list[Path]:
    """把 Markdown 文件转换为 docx / pdf / 自包含 markdown，返回生成的文件路径列表。

    Args:
        md_path:  Markdown 文件路径；留空回退到 .env 的 PAPER_TOOLS_MD_INPUT。
        fmt:      输出格式 docx / pdf / docx_pdf / portable / all，可用逗号组合
                  （如 docx,portable）。portable = 自包含 markdown，图片内联为
                  base64，便于直接发给他人。留空回退到 .env 的
                  PAPER_TOOLS_MD_EXPORT_FORMATS（默认 docx_pdf）。
        out_dir:  输出目录；留空与源文件同目录。
        settings: 全局配置（不传则自动读取）。
    """
    settings = settings or get_settings()
    md_path = md_path or settings.md_input
    if not md_path:
        raise FileNotFoundError(
            "未指定 Markdown 文件：请传入路径，或在 .env 设置 PAPER_TOOLS_MD_INPUT")
    md_path = Path(md_path)
    if not md_path.exists():
        raise FileNotFoundError(f"Markdown 文件不存在: {md_path}")
    text = md_path.read_text(encoding="utf-8")

    fmt = (fmt or settings.md_export_formats or "docx_pdf").strip().lower().replace(",", "_")
    if fmt == "all":
        fmt = "docx_pdf"
    want_docx = "docx" in fmt
    want_pdf = "pdf" in fmt
    want_portable = "portable" in fmt
    if not (want_docx or want_pdf or want_portable):
        raise ValueError(
            f"不支持的导出格式: {fmt}（可选 docx / pdf / docx_pdf / portable / all，"
            "可用逗号组合，如 docx,portable）")

    out_dir = Path(out_dir) if out_dir else md_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # 文件名长度保护：源文件名来自用户，超长 stem 会使输出路径超出
    # Windows MAX_PATH（260）。截断到 120（与 arxiv 的 _safe_filename 一致）。
    stem = md_path.stem[:120].rstrip(". ") or "document"

    # 缓存目录：网络图片下载 + 公式渲染 PNG，均放在源文件旁，便于复用
    cache_root = md_path.parent / f"{md_path.stem}.md_export"
    ctx = ConvertCtx(
        source_dir=md_path.parent,
        images_dir=cache_root / "images",
        math_dir=cache_root / "math",
        diagram_dir=cache_root / "diagram",
        settings=get_settings(),
    )

    results: list[Path] = []
    if want_docx or want_pdf:
        blocks = parse_markdown(text)
        if want_docx:
            docx_out = out_dir / f"{stem}.docx"
            if confirm_overwrite(docx_out, settings=settings):
                results.append(blocks_to_docx(blocks, docx_out, ctx))
        if want_pdf:
            pdf_out = out_dir / f"{stem}.pdf"
            if confirm_overwrite(pdf_out, settings=settings):
                results.append(blocks_to_pdf(blocks, pdf_out, ctx))
    if want_portable:
        # 延迟导入：portable 反向依赖本模块的图片解析，模块级导入会成环
        from .portable import write_portable

        portable_out = out_dir / f"{stem}.portable.md"
        if confirm_overwrite(portable_out, settings=settings):
            results.append(write_portable(text, portable_out, ctx))
    return results
