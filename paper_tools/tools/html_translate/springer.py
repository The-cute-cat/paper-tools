"""解析 Springer Nature Link（link.springer.com）文章 HTML，产出统一的 Block 列表。

link.springer.com 的文章页使用一套稳定的 ``c-article-*`` 语义类名：

  - ``h1.c-article-title``                    标题
  - ``ul.c-article-author-list``              作者（``a[data-test=author-name]``）
  - ``ol.c-article-author-affiliation__list`` 机构（``li#Aff1`` …）
  - ``div#Abs1-section``                      摘要（``h2#Abs1`` + ``div#Abs1-content``）
  - ``div.c-article-section``                 章节（``h2.c-article-section__title`` + 内容）
  - ``h3.c-article__sub-heading``             子标题（h4 带 ``--small``）
  - ``div.c-article-section__figure``         图（``picture > img`` + 图注 + 描述）
  - ``div.c-article-table``                   表（正文页只放题注，数据在 ``/tables/N`` 独立页）
  - ``ol.c-article-footnote--listed``         脚注定义（``li#Fn1``，正文以 ``sup>a[href=#Fn1]`` 引用）
  - ``ul.c-article-references``               参考文献（``p.c-article-references__text``，id=ref-CRn）

与 ar5iv 一样复用 ``parser.py`` 里的通用设施（Block、data-zh-id 标注、锚点集合、
表格转 markdown/HTML），从而与翻译流水线无缝衔接。
"""

import re
from pathlib import Path
from typing import Callable, Optional

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString, PageElement

from . import parser as _ax
from .parser import (
    Block,
    _assign_html_ids,
    _collect_table_text,
    _html_id_of,
    _html_img,
    _img_size,
    _normalize_tex_for_katex,
    _strip_displaystyle,
)
from ...logging_setup import get_logger

logger = get_logger()

# 纯装饰 / 无障碍隐藏文本（"Footnote "、"ORCID: " 等），渲染时必须剔除。
_HIDDEN_CLASSES = {"u-visually-hidden", "u-js-hide", "u-hide", "u-hide-print", "u-hide-screen"}

# 元数据型章节（作者信息、伦理声明、版权、引用信息等）不是论文正文，跳过。
_META_SECTION_IDS = {
    "notes", "Bib1", "author-information", "ethics",
    "additional-information", "rightslink", "article-info",
}


def _attr(v: object) -> str:
    """把 bs4 属性值收窄为字符串。"""
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)) and v and isinstance(v[0], str):
        return v[0]
    return ""


def _frag_of(href: str) -> str:
    """取 URL 的 fragment（``...#ref-CR6`` → ``ref-CR6``）。"""
    return href.split("#", 1)[1] if "#" in href else ""


def _abs_url(href: str, base_url: str) -> str:
    """把相对 / 协议相对链接补全为绝对 URL。"""
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return base_url.rstrip("/") + href
    return href


def _build_anchor_sets_springer(soup: "BeautifulSoup") -> tuple[set[str], set[str]]:
    """收集 (存在的 id, 被内部链接引用的 id)。

    Springer 正文引用形如 ``href="/article/<doi>#ref-CR6"``（含路径），不能只认
    ``href`` 以 ``#`` 开头的链接，因此这里按 URL 的 fragment 判断：只要 fragment
    命中文档内某个元素 id，就视为内部引用。
    """
    existing: set[str] = set()
    for tag in soup.find_all(id=True):
        tid = _attr(tag.get("id"))
        if tid:
            existing.add(tid)
    referenced: set[str] = set()
    for a in soup.find_all("a", href=True):
        frag = _frag_of(_attr(a.get("href")))
        if frag and frag in existing:
            referenced.add(frag)
    return existing, referenced


def _mathml_tex(math_tag: Tag) -> str:
    """从 MathML 公式提取 LaTeX 源码（优先 x-tex annotation，其次 alttext）。"""
    ann = math_tag.find("annotation", attrs={"encoding": "application/x-tex"})
    if ann is not None and ann.string:
        return _normalize_tex_for_katex(_strip_displaystyle(ann.string.strip()))
    alt = _attr(math_tag.get("alttext"))
    if alt:
        return _normalize_tex_for_katex(_strip_displaystyle(alt.strip()))
    return ""


def _inline(node: Tag | NavigableString, *, rich: bool,
            base_url: str = "https://link.springer.com/") -> str:
    """把 Springer 行内内容渲染为 markdown 片段。

    rich=True  → 保留链接 / 粗体 / 斜体等 markdown 标记（用于原文输出与 raw）。
    rich=False → 仅保留文本与 ``$…$`` 公式（用于送 LLM 的待译文本，避免 URL 被模型改写）。

    脚注标记 ``<sup><a href="#Fn1">…</a></sup>`` 一律转为 Markdown footnote ``[^1]``。
    """

    def render(nd: PageElement) -> str:
        if isinstance(nd, NavigableString):
            return str(nd)
        if not isinstance(nd, Tag):
            return ""
        cls = nd.get("class") or []
        if any(c in _HIDDEN_CLASSES for c in cls):
            return ""
        name = nd.name
        if name in ("script", "style", "svg", "use", "noscript", "button", "iframe", "img"):
            return ""
        if name == "math":
            tex = _mathml_tex(nd)
            return f"${tex}$" if tex else nd.get_text(" ", strip=True)
        if name == "br":
            return " "
        if name == "a":
            href = _attr(nd.get("href"))
            m = re.search(r"#?(Fn\d+)$", href)
            if m:
                # 脚注标记：<a href="#Fn1"> → [^1]
                return f"[^{m.group(1)[2:]}]"
            inner = _children(nd)
            if rich and href:
                frag = _frag_of(href)
                if frag and frag in _ax._REFERENCED_ANCHORS:
                    # 内部引用（文献 / 图表 / 章节）：指向会输出 <a id> 的目标。
                    return f"[{inner}](#{frag})"
                if not href.startswith("#"):
                    return f"[{inner}]({_abs_url(href, base_url)})"
                return inner
            return inner
        if name in ("b", "strong"):
            inner = _children(nd)
            return f"**{inner}**" if rich else inner
        if name in ("i", "em", "cite"):
            inner = _children(nd)
            return f"*{inner}*" if rich else inner
        if name == "sub":
            t = nd.get_text(" ", strip=True)
            return f"$_{{{t}}}$" if t else ""
        if name == "sup":
            # 脚注上标：``<sup><a href="#Fn1">…</a></sup>`` → 交给 a 分支产出 [^1]
            if nd.find("a", href=re.compile(r"#Fn\d+$")) is not None:
                return _children(nd)
            t = nd.get_text(" ", strip=True)
            return f"$^{{{t}}}$" if t else ""
        if name in ("p", "div", "li", "ul", "ol", "blockquote", "section"):
            inner = _children(nd)
            return f" {inner} " if inner else ""
        return _children(nd)

    def _children(tag: Tag) -> str:
        return "".join(render(c) for c in tag.children)

    text = render(node).replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _text_of(node: Optional[Tag], *, rich: bool, base_url: str) -> str:
    if node is None:
        return ""
    return _inline(node, rich=rich, base_url=base_url)


def _clean_heading(node: Tag, *, rich: bool, base_url: str) -> str:
    """章节标题：剔除无障碍隐藏文本，保留章节编号（如 "1 Introduction"、"Appendix A …"）。"""
    clone = BeautifulSoup(str(node), "html.parser")
    for junk in clone.find_all(class_=re.compile(r"u-visually-hidden|u-js-hide|u-hide")):
        junk.decompose()
    root = clone.find(node.name) or clone
    return _text_of(root, rich=rich, base_url=base_url)


def _anchor_meta(el: Optional[Tag]) -> dict:
    """构造 meta：带 html_id，并在该元素 id 被正文引用时补 anchor。"""
    meta: dict = {"html_id": _html_id_of(el) if el is not None else None}
    if el is not None:
        eid = _attr(el.get("id"))
        if eid and eid in _ax._REFERENCED_ANCHORS:
            meta["anchor"] = eid
    return meta


def parse_springer_html(
    html_path: str | Path,
    *,
    img_mapping: Optional[dict[str, str]] = None,
    base_url: str = "https://link.springer.com/",
    article_url: Optional[str] = None,
    table_fetcher: Optional[Callable[[str], str]] = None,
) -> tuple[list[Block], "BeautifulSoup"]:
    """解析 Springer Nature Link 文章 HTML，返回 (blocks, soup)。

    img_mapping:   本地图片模式下的 原始src -> 本地相对路径 映射；不传则图片保持网络 URL。
    base_url:      站点根 URL，用于把相对 / 协议相对链接补全为绝对 URL。
    article_url:   文章页 URL（``.../article/<doi>``）。表格数据在 ``<article_url>/tables/N``
                   独立页，故抓取表格时需要它；缺省用 base_url。
    table_fetcher: 可选回调（如 ``download_text``）。正文页通常不含表格数据，传入时按需
                   抓取并解析；抓取失败则退化为「题注 + 独立表页链接」。
    """
    html_path = Path(html_path)
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    img_mapping = img_mapping or {}
    base_url = base_url or "https://link.springer.com/"

    # 1) 去噪：推荐阅读、相关学科、作者档案、脚本样式等。
    #    注意：不要删除 <header>——文章标题/作者列表就在 <header class=""> 里。
    for sel in (".c-article-recommendations", ".app-explore-related-subjects",
                "#researcher-profile-container", "script", "style", "noscript",
                ".c-article-metrics-bar", ".c-article-identifiers", ".eds-c-header"):
        for el in soup.select(sel):
            el.decompose()

    # 2) 图片归一化：协议相对 //media... → https://media...；本地模式命中映射换本地路径。
    def _norm_src(raw: str) -> str:
        if img_mapping and raw in img_mapping:
            return img_mapping[raw]
        if raw.startswith("//"):
            return "https:" + raw
        if raw.startswith("http"):
            return raw
        return base_url.rstrip("/") + "/" + raw.lstrip("/")

    for img in soup.find_all("img"):
        src = _attr(img.get("src"))
        if src:
            img["src"] = _norm_src(src)

    # 3) 复用 ar5iv 解析器的通用设施：锚点集合 + data-zh-id 标注。
    #    Springer 的正文链接形如 ``/article/<doi>#ref-CR6``（带路径），需按 fragment
    #    识别内部引用，故这里用专门的集合构造。
    _ax._EXISTING_IDS, _ax._REFERENCED_ANCHORS = _build_anchor_sets_springer(soup)
    _ax._BIB_MAP = {}
    _assign_html_ids(soup)

    ctx = {"base_url": base_url, "table_fetcher": table_fetcher,
           "article_url": (article_url or base_url).rstrip("/")}
    blocks: list[Block] = []

    # ---- 标题 ----
    title_el = soup.find("h1", class_=re.compile(r"c-article-title")) or soup.find("h1")
    title_text = _text_of(title_el, rich=False, base_url=base_url) if title_el is not None else ""
    if not title_text:
        meta_title = soup.find("meta", attrs={"name": "citation_title"})
        title_text = _attr(meta_title.get("content")) if meta_title is not None else ""
    if title_text:
        blocks.append(Block(kind="title", level=1, text=title_text, raw=title_text,
                            meta={"html_id": _html_id_of(title_el) if title_el is not None else None}))

    # ---- 作者 / 机构 ----
    _emit_authors(soup, blocks, base_url=base_url)

    # ---- 关键词 ----
    _emit_keywords(soup, blocks, base_url=base_url)

    # ---- 摘要 ----
    abs_content = soup.find(id="Abs1-content")
    if abs_content is not None:
        blocks.append(Block(kind="heading", level=2, text="Abstract", raw="Abstract",
                            meta=_anchor_meta(soup.find(id="Abs1")) | {"html_id": _html_id_of(abs_content)}))
        for p in abs_content.find_all("p", recursive=False):
            _emit_paragraph(p, blocks, ctx, section="abstract")

    # ---- 正文章节（含附录 / 致谢），按文档顺序 ----
    for sec in soup.find_all(class_="c-article-section"):
        title_el = sec.find(class_="c-article-section__title")
        tid = _attr(title_el.get("id")) if title_el is not None else ""
        if tid == "Abs1" or tid in _META_SECTION_IDS:
            continue
        content = sec.find(class_="c-article-section__content")
        if content is None:
            continue
        heading = _clean_heading(title_el, rich=True, base_url=base_url) if title_el is not None else ""
        if heading:
            blocks.append(Block(kind="heading", level=2, text=heading, raw=heading,
                                meta=_anchor_meta(title_el)))
        _walk_content(content, blocks, ctx)

    # ---- 参考文献 ----
    _emit_references(soup, blocks, base_url=base_url)

    # ---- 脚注定义 ----
    _emit_footnotes(soup, blocks, base_url=base_url)

    return blocks, soup


def _emit_authors(soup: "BeautifulSoup", blocks: list[Block], *, base_url: str) -> None:
    """作者列表：姓名 + 机构编号 + 通讯作者标记。"""
    ul = soup.find("ul", class_=re.compile(r"c-article-author-list"))
    if ul is None:
        return

    lines: list[str] = []
    for li in ul.find_all("li", recursive=False):
        a = li.find("a", attrs={"data-test": "author-name"}) or li.find("a")
        name = _text_of(a, rich=False, base_url=base_url) if a is not None else ""
        if not name:
            continue
        affs: list[str] = []
        for link in li.find_all("a", href=True):
            m = re.search(r"#(Aff\d+)$", _attr(link.get("href")))
            if m and m.group(1) not in affs:
                affs.append(m.group(1))
        extra: list[str] = []
        if affs:
            extra.append(", ".join(affs))
        if a is not None and _attr(a.get("data-corresp-id")):
            extra.append("Corresponding author")
        suffix = f" ({'; '.join(extra)})" if extra else ""
        lines.append(f"{name}{suffix}")

    if lines:
        text = "; ".join(lines)
        blocks.append(Block(kind="authors", text=text, raw=text,
                            meta={"html_id": _html_id_of(ul), "lines": lines}))

    # 机构说明逐条输出为列表项，便于读者与作者后的编号（Aff1/Aff2…）对照。
    for li in soup.find_all("li", id=re.compile(r"^Aff\d+$")):
        content = _text_of(li, rich=False, base_url=base_url)
        if not content:
            continue
        blocks.append(Block(kind="list_item", text=content,
                            raw=f"- {_attr(li.get('id'))}: {content}",
                            meta={"html_id": _html_id_of(li)}))


def _emit_keywords(soup: "BeautifulSoup", blocks: list[Block], *, base_url: str) -> None:
    ul = soup.find("ul", class_=re.compile(r"subject-list|keyword"))
    if ul is None:
        return
    kws = [_text_of(li, rich=False, base_url=base_url) for li in ul.find_all("li", recursive=False)]
    kws = [k for k in kws if k]
    if not kws:
        return
    value = "; ".join(kws)
    blocks.append(Block(kind="paragraph", text=f"Keywords: {value}",
                        raw=f"**Keywords:** {value}",
                        meta={"html_id": _html_id_of(ul)}))


def _emit_paragraph(el: Tag, blocks: list[Block], ctx: dict,
                    section: Optional[str] = None) -> None:
    rich = _inline(el, rich=True, base_url=ctx["base_url"])
    if not rich:
        return
    plain = _inline(el, rich=False, base_url=ctx["base_url"])
    meta = {"html_id": _html_id_of(el)}
    if section:
        meta["section"] = section
    blocks.append(Block(kind="paragraph", text=plain, raw=rich, meta=meta))


def _walk_content(content: Tag, blocks: list[Block], ctx: dict, depth: int = 0) -> None:
    """按文档顺序遍历章节内容容器。"""
    if depth > 6:
        return
    for child in content.find_all(recursive=False):
        name = child.name
        if name in ("script", "style", "br", "noscript"):
            continue
        cls = set(child.get("class") or [])
        if cls & _HIDDEN_CLASSES:
            continue
        if name in ("h2", "h3", "h4"):
            level = {"h2": 3, "h3": 3, "h4": 4}[name]
            heading = _clean_heading(child, rich=True, base_url=ctx["base_url"])
            if heading:
                blocks.append(Block(kind="heading", level=level, text=heading, raw=heading,
                                    meta=_anchor_meta(child)))
            continue
        if name == "p":
            if "c-article-rights" in cls:
                continue
            _emit_paragraph(child, blocks, ctx)
            continue
        if name in ("ul", "ol"):
            if any("c-article-footnote" in c for c in cls):
                continue  # 脚注定义单独收集
            _emit_list(child, blocks, ctx)
            continue
        if name == "div":
            if "c-article-table" in cls:
                _emit_table(child, blocks, ctx)
            elif any(c.startswith("c-article-section__figure") for c in cls):
                _emit_figure(child, blocks, ctx)
            elif "c-bibliographic-information" in cls:
                continue
            else:
                _walk_content(child, blocks, ctx, depth + 1)
            continue
        if name == "figure":
            _emit_figure(child, blocks, ctx)
            continue
        if name == "table":
            _emit_table(child, blocks, ctx)
            continue
        if name in ("section", "article"):
            _walk_content(child, blocks, ctx, depth + 1)
            continue


def _emit_list(lst: Tag, blocks: list[Block], ctx: dict) -> None:
    style_none = "u-list-style-none" in (lst.get("class") or [])
    for li in lst.find_all("li", recursive=False):
        rich = _inline(li, rich=True, base_url=ctx["base_url"])
        plain = _inline(li, rich=False, base_url=ctx["base_url"])
        if not rich:
            continue
        if style_none:
            # ol.u-list-style-none 的序号是字面文本（"1. …"），与 markdown 项目符号重复，剥掉。
            rich = re.sub(r"^\d+\.\s+", "", rich)
            plain = re.sub(r"^\d+\.\s+", "", plain)
        blocks.append(Block(kind="list_item", text=plain, raw="- " + rich,
                            meta={"html_id": _html_id_of(li)}))


def _emit_figure(div: Tag, blocks: list[Block], ctx: dict) -> None:
    """图：``<picture><img>`` + 图注（b#FigN）+ 描述。"""
    img = div.find("img")
    caption_nodes: list[str] = []
    label_el = div.find("b", attrs={"data-test": "figure-caption-text"})
    if label_el is not None:
        caption_nodes.append(_text_of(label_el, rich=False, base_url=ctx["base_url"]))
    desc = div.find(class_=re.compile(r"figure-description"))
    if desc is not None:
        d = _text_of(desc, rich=True, base_url=ctx["base_url"])
        if d:
            caption_nodes.append(d)
    caption = " ".join(t for t in caption_nodes if t).strip()

    src = _attr(img.get("src")) if img is not None else ""
    size = _img_size(img) if img is not None else None
    meta: dict = {"src": src, "local_src": src, "caption": caption, "html_id": _html_id_of(div)}
    if label_el is not None:
        fid = _attr(label_el.get("id"))
        if fid and fid in _ax._REFERENCED_ANCHORS:
            meta["anchor"] = fid
    if src:
        raw = _html_img("figure", src, size)
        if caption:
            raw += f"\n\n> {caption}"
        blocks.append(Block(kind="figure", text=caption, raw=raw, meta=meta))
    elif caption:
        blocks.append(Block(kind="figure", text=caption, raw=f"> {caption}", meta=meta))


def _emit_table(div: Tag, blocks: list[Block], ctx: dict) -> None:
    """表：正文页通常只有题注，数据需从 ``/tables/N`` 独立页抓取。"""
    cap_el = div.find("b", attrs={"data-test": "table-caption"})
    cap_text = _text_of(cap_el, rich=True, base_url=ctx["base_url"]) if cap_el is not None else ""

    tbl = div.find("table")
    table_md = _collect_table_text(tbl) if tbl is not None else _fetch_table_md(div, ctx)

    meta: dict = {"caption": cap_text, "table_md": table_md, "html_id": _html_id_of(div)}
    if cap_el is not None:
        tid = _attr(cap_el.get("id"))
        if tid and tid in _ax._REFERENCED_ANCHORS:
            meta["anchor"] = tid

    if table_md:
        raw = table_md + (f"\n\n> {cap_text}" if cap_text else "")
        blocks.append(Block(kind="table", text=cap_text, raw=raw, meta=meta))
        return

    # 退化为「题注 + 独立表页链接」，保证信息不丢且可跳转。
    n = re.search(r"(\d+)$", _attr(div.get("id")))
    label = cap_text or (f"Table {n.group(1)}" if n else "Table")
    if n:
        raw = f"> [{label}]({ctx['article_url']}/tables/{n.group(1)})"
    else:
        raw = f"> {label}"
    blocks.append(Block(kind="table", text=cap_text, raw=raw, meta=meta))


def _fetch_table_md(div: Tag, ctx: dict) -> str:
    """从 ``/tables/N`` 独立页抓取并转换为 markdown / HTML 表格。"""
    fetcher = ctx.get("table_fetcher")
    n = re.search(r"(\d+)$", _attr(div.get("id")))
    if fetcher is None or not n:
        return ""
    url = f"{ctx['article_url']}/tables/{n.group(1)}"
    try:
        html = fetcher(url)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"  表格 {n.group(1)} 抓取失败，退化为题注+链接：{e}")
        return ""
    t = BeautifulSoup(html, "html.parser").find("table")
    if t is None:
        logger.warning(f"  表格 {n.group(1)} 页面未找到 <table>，退化为题注+链接")
        return ""
    logger.info(f"  已抓取表格 {n.group(1)} 数据（{len(t.find_all('tr'))} 行）")
    return _collect_table_text(t)


def _emit_references(soup: "BeautifulSoup", blocks: list[Block], *, base_url: str) -> None:
    ul = soup.find("ul", class_=re.compile(r"c-article-references"))
    if ul is None:
        return
    blocks.append(Block(kind="heading", level=2, text="References", raw="References",
                        meta=_anchor_meta(soup.find(id="Bib1")) | {"html_id": _html_id_of(ul)}))

    def _ref_no(li: Tag) -> int:
        p = li.find("p", class_=re.compile(r"c-article-references__text"))
        m = re.search(r"ref-CR(\d+)", _attr(p.get("id")) if p is not None else "")
        return int(m.group(1)) if m else 10 ** 6

    for li in sorted(ul.find_all("li", recursive=False), key=_ref_no):
        p = li.find("p", class_=re.compile(r"c-article-references__text")) or li.find("p")
        if p is None:
            continue
        rich = _inline(p, rich=True, base_url=base_url)
        if not rich:
            continue
        plain = _inline(p, rich=False, base_url=base_url)
        meta: dict = {"html_id": _html_id_of(li)}
        rid = _attr(p.get("id"))
        if rid and rid in _ax._REFERENCED_ANCHORS:
            meta["anchor"] = rid
        blocks.append(Block(kind="list_item", text=plain, raw="- " + rich, meta=meta))


def _emit_footnotes(soup: "BeautifulSoup", blocks: list[Block], *, base_url: str) -> None:
    ol = soup.find("ol", class_=re.compile(r"c-article-footnote--listed"))
    if ol is None:
        return
    defs: list[tuple[int, str]] = []
    for li in ol.find_all("li", recursive=False):
        m = re.match(r"Fn(\d+)$", _attr(li.get("id")))
        if not m:
            continue
        content = _inline(li, rich=True, base_url=base_url)
        if content:
            defs.append((int(m.group(1)), content))
    for n, text in sorted(defs):
        blocks.append(Block(kind="footnote", text=text, raw=f"[^{n}]: {text}",
                            meta={"mark": str(n)}))
