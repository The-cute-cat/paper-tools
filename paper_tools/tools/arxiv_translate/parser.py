"""解析 ar5iv (LaTeXML) 生成的 arxiv HTML 论文，提取结构化内容。

ar5iv HTML 使用一套稳定的语义 class，例如：
  - ltx_title_document : 论文标题
  - ltx_title_abstract : 摘要标题
  - ltx_section / ltx_subsection / ltx_subsubsection : 章节/子章节
  - ltx_title_section / ltx_title_subsection ... : 各级标题
  - ltx_para > ltx_p : 段落
  - ltx_equation : 行间公式
  - ltx_Math (inline) : 行内公式
  - ltx_figure / ltx_table : 图表
  - ltx_itemize / ltx_enumerate > ltx_item : 列表

公式以 LaTeX 形式保存在 <annotation encoding="application/x-tex"> 中。
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from bs4 import BeautifulSoup, NavigableString, Tag

from ...config import get_settings


# ============ 引用搜索引擎 ============
# 默认 Bing 国内可直连；Google/Bing/DuckDuckGo/Semantic Scholar/arXiv 全部可切换。
# 通过环境变量 PAPER_TOOLS_CITE_SEARCH 设置，main.py 里也可以覆盖。
_CITE_SEARCH_URL_TEMPLATES: dict[str, str] = {
    # 国际版 Bing（国内可访问，自动跳转 cn.bing.com）
    "bing": "https://www.bing.com/search?q={query}",
    "google": "https://scholar.google.com/scholar?q={query}",
    "duckduckgo": "https://duckduckgo.com/?q={query}",
    "semantic_scholar": "https://www.semanticscholar.org/search?q={query}",
    "arxiv": "https://arxiv.org/?query={query}",
}


def _cite_search_url(query: str, engine: Optional[str] = None) -> str:
    """根据配置的搜索引擎构造引用查询 URL。"""
    if engine is None:
        engine = get_settings().cite_search_engine
    template = _CITE_SEARCH_URL_TEMPLATES.get(engine, _CITE_SEARCH_URL_TEMPLATES["bing"])
    return template.format(query=quote(query))


@dataclass
class Block:
    """一个内容块，对应 markdown 中的一段可翻译单元。"""
    kind: str  # "heading" | "paragraph" | "equation" | "figure" | "table" | "list_item" | "title"
    level: int = 0          # heading 的 markdown 级别
    text: str = ""          # 需要翻译的纯英文文本
    raw: str = ""           # 已含 markdown 标记（公式/链接保留）的文本
    meta: dict = field(default_factory=dict)


def _as_str(v: object) -> Optional[str]:
    """BeautifulSoup get() 返回值可能是 list，收窄为 str。"""
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)) and v and isinstance(v[0], str):
        return v[0]
    return None


def _strip_displaystyle(tex: str) -> str:
    """去掉 LaTeXML 给每个 math 注释添加的 '\\displaystyle' 前缀。

    KaTeX 在 $$ 块中默认就是 display 模式，\\displaystyle 是冗余且在行内/aligned 中非法。
    """
    return re.sub(r"^\s*\\displaystyle\s*", "", tex)


def _tex_of(math_tag: Tag) -> str:
    """从 math 标签中提取 LaTeX 源码。

    LaTeXML 在每个 annotation 前面加 '\\displaystyle' 前缀。KaTeX 在行内 $..$ 中
    不支持该命令，因此默认去除；display 场景下也冗余且在 aligned 等环境中非法。
    """
    ann = math_tag.find("annotation", attrs={"encoding": "application/x-tex"})
    if ann and ann.string:
        return _strip_displaystyle(ann.string.strip())
    alt = _as_str(math_tag.get("alttext"))
    return _strip_displaystyle(alt.strip()) if alt else ""


def _looks_like_inline_math(tex: str) -> bool:
    """启发式：公式内容是否看起来像行内公式（而非行间公式）。

    ar5iv 偶尔会把短变量（y, x, d, l, s, v, \\mathbf{d} 等）标为
    display="block"，导致 parser 输出 $$y$$ 而非 $y$。这些 $$ 块未配对时会
    打断 protect_math 的正则匹配链，使英文原文泄露进 LLM 输入。

    判定逻辑：内容 ≤ 25 字符且不含显示环境标记 → 行内。
    """
    stripped = tex.strip()
    if len(stripped) > 25:
        return False
    # 含这些命令的公式大概率是真正的行间公式
    if re.search(
        r"\\(?:begin|aligned|sum|int|frac|prod|lim|displaystyle|choose|binom|cases|matrix)",
        stripped,
    ):
        return False
    return True


def _rich_text(tag: Tag) -> str:
    """把含行内公式/强调/链接的片段转为保留公式的 markdown 文本。"""
    parts: list[str] = []

    def walk(node):
        if isinstance(node, NavigableString):
            parts.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        name = node.name
        cls = node.get("class") or []
        if "ltx_note" in cls and "ltx_role_footnote" in cls:
            # 脚注：只保留上标标记 [n]，不把脚注正文插入段落内部（避免重复数字与错位）
            mark = node.find(class_="ltx_note_mark")
            if mark:
                mtxt = (mark.get_text() or "").strip()
                if mtxt:
                    parts.append(f"[{mtxt}]")
            return
        if "ltx_cite" in cls:
            # 参考文献引用：
            #  - 把论文名换成搜索引擎链接；
            #  - 显示文本按 cite_display_mode 选择：
            #      "short" → 只显示作者年份（短链），论文名作为 hover 提示（浏览器原生 title）
            #      "title" → 显示作者年份 + 论文名（信息全，但长）
            display_mode = get_settings().cite_display_mode
            for c in node.children:
                if isinstance(c, Tag) and c.name == "a" and "ltx_ref" in (c.get("class") or []):
                    title = _as_str(c.get("title")) or ""
                    visible = c.get_text().strip()
                    if title:
                        # 去掉 title 末尾的 bibkey 年份后缀，如 ", 2024a" → "Title"
                        clean_title = re.sub(r",\s*\d{4}[a-z]?\s*$", "", title).strip()
                        url = _cite_search_url(clean_title)
                        # 去掉 visible 末尾多余逗号（避免 "Kuhn et al.,, Title"）
                        vis = visible.rstrip(", ").strip()
                        if display_mode == "title":
                            display = f"{vis}, {clean_title}" if vis else clean_title
                        else:  # "short"
                            display = vis if vis else clean_title
                        # 浏览器原生 title 属性显示为悬浮提示，论文名挂在这里
                        parts.append(f"[{display}]({url} \"{clean_title}\")")
                    else:
                        href = _as_str(c.get("href")) or ""
                        parts.append(f"[{visible}]({href})" if href else visible)
                else:
                    # 其他文本节点（如 "(Shi et al., "、"；"、")"）原样
                    if isinstance(c, NavigableString):
                        parts.append(str(c))
                    elif isinstance(c, Tag):
                        for cc in c.children:
                            walk(cc)
            return
        if "ltx_Math" in cls:
            tex = _tex_of(node)
            if tex:
                disp = _as_str(node.get("display")) == "block" and not _looks_like_inline_math(tex)
                parts.append(f"\n$$\n{tex}\n$$\n" if disp else f"${tex}$")
            return
        if name in ("sub", "sup"):
            # 数学上下标（如 HD<sub>max</sub> → $HD_{max}$），ar5iv 常将表头指标的下标
            # 写为 <sub>/<sup> 而非 <math>，若不处理会变成纯文本混入译文。
            # 取内层文本作为上下标内容，整体包成行内公式以保持渲染与不被翻译。
            inner = node.get_text().strip()
            if not inner:
                return
            sym = "_" if name == "sub" else "^"
            parts.append(f"${sym}{{{inner}}}$")
            return
        if name == "br":
            parts.append("\n")
            return
        if "ltx_emph" in cls or name in ("em", "i"):
            parts.append("*")
            for c in node.children:
                walk(c)
            parts.append("*")
            return
        if "ltx_font_bold" in cls or name in ("b", "strong"):
            parts.append("**")
            for c in node.children:
                walk(c)
            parts.append("**")
            return
        if name == "a":
            href = _as_str(node.get("href")) or ""
            parts.append("[")
            for c in node.children:
                walk(c)
            parts.append(f"]({href})" if href else "]")
            return
        for c in node.children:
            walk(c)

    walk(tag)
    text = "".join(parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    # 公式两侧保留单个空格，避免与正文中英文字粘连（如 "parameters $\theta$ define"）
    text = re.sub(r"\s*\$(\s|\$)", lambda m: " $" + m.group(1), text)
    text = re.sub(r"(\$|\s)\$\s*", lambda m: m.group(1) + "$ ", text)
    text = re.sub(r" +", " ", text)
    # 修复：合并紧贴粗体标签的公式前缀（如 **H**$_{tok}$ → **$H_{tok}$**）
    # ar5iv 中表头常将指标名用 <strong>H</strong><sub>tok</sub> 分开渲染，
    # 导致 _rich_text 输出 **H**$_{tok}$ —— 粗体 H 和公式下标拆成两段，
    # Markdown 会把 ** 当作粗体边界而 $...$ 被拆散。这里把 1-3 个字母的粗体前缀
    # 移入紧随的 $_{...}$ / $^{...}$ 公式内，粗体 $...$ 包住合并后的完整公式。
    # 捕获：\1=前缀字母，\2=公式内部内容（去掉外层的 $...$）
    # 首字符须是 \ / _ / ^ / {，确保捕获的是 LaTeX（如 $_{max}$ / $^2$ / $\theta$）
    text = re.sub(
        r"\*\*([A-Za-z0-9_]{1,3})\*\*\$([\\_{}^\{][^$\n]+?)\$",
        r"**$\1\2$**",
        text,
    )
    return text.strip()


def _plain_text(node, *, wrap_math: bool = False) -> str:
    """遍历节点，提取纯文本。

    wrap_math=False（默认）：行内公式以裸 LaTeX 源码形式返回（如 \\theta、\\mathcal{Y}），
        用于标题/概要/图注等"向用户展示"的位置，避免肉眼看到 $...$。
    wrap_math=True：行内公式包成 $...$、行间公式包成 $$...$$，用于送入 LLM 的
        待译文本。配合 translator.protect_math 的 $...$ 规则，可在边界（$）上准
        确识别公式，避免 _MATH_BARE_RE 兜底正则误把 \\mathcal{S} denotes 之类
        "LaTeX 命令 + 普通词" 的混合串吞掉。
    """
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""
    cls = node.get("class") or []
    name = node.name
    if "ltx_Math" in cls:
        tex = _tex_of(node)
        if not tex:
            return ""
        if not wrap_math:
            return tex
        # display="block" 但内容像行内短变量（如 y, x, d, l）→ 强制 inline，
        # 避免 $$y$$ / $$\mathbf{d}$$ 打乱 protect_math 的正则匹配链
        disp = _as_str(node.get("display")) == "block" and not _looks_like_inline_math(tex)
        if disp:
            return f"\n$$\n{tex}\n$$\n"
        return f"${tex}$"
    if name in ("sub", "sup"):
        # ar5iv 中部分指标下标用 <sub>/<sup> 而非 <math> 渲染（如 HD<sub>max</sub>）。
        # _rich_text 已正确处理为 $_{max}$，这里必须同步，否则 LLM 会看到裸 "HDmax"
        # 而无法区分下标语义，导致翻译后格式错乱（如 "HD↓max{}_{\\max}\\downarrow"）。
        inner = node.get_text().strip()
        if not inner:
            return ""
        sym = "_" if name == "sub" else "^"
        if wrap_math:
            return f"${sym}{{{inner}}}$"
        return f"{sym}{{{inner}}}"
    if "ltx_note" in cls and "ltx_role_footnote" in cls:
        mark = node.find(class_="ltx_note_mark")
        mtxt = (mark.get_text() or "").strip() if mark else ""
        return f"[{mtxt}]" if mtxt else ""
    return "".join(_plain_text(c, wrap_math=wrap_math) for c in node.children)


def _plain_text_for_translation(node) -> str:
    """翻译专用纯文本：行内/行间公式包成 $...$ / $$...$$。

    与 _plain_text 的差异：所有 inline 公式以 $...$ 形式保留，让
    LLMTranslator.protect_math 通过 _MATH_INLINE_RE 在 $...$ 边界上精确
    整段保护，避免 _MATH_BARE_RE 兜底把 "\\mathcal{S} denotes"、
    "p_{\\theta}(y\\mid x)" 这类 LaTeX 命令与普通词混合串误吞或漏保护，
    导致 LLM 把公式源码当普通文本改写/翻译。

    后处理：
      1. "H$...$" 紧邻单字符前缀 → 把 H 移入公式内（"H$_{\\text{tok}}$" →
         "$H_{\\text{tok}}$"），让 LLM 把整段当一个公式保护；
      2. $ 与汉字之间补空格，避免中英文混淆；
      3. 行间公式用换行隔离，KaTeX 自动进入 display 模式。

    仅影响送入 LLM 的 prompt，不影响最终 markdown 渲染（markdown 用
    _rich_text 输出，KaTeX 仍正确显示 $H_{\\text{tok}}$ / $x$）。
    """
    raw = _plain_text(node, wrap_math=True)
    text = raw
    # 步骤 1：合并紧邻 inline 公式的 ASCII 字母前缀（H/x/y/HD 等指标代号）
    # 到公式 LaTeX 源码内，让 LLMTranslator 通过 _MATH_INLINE_RE 整段保护。
    #
    # 约束：
    #   - prefix 最多 3 个 ASCII 字母/数字（H、HD、ASR 等），太长说明不是指标代号；
    #   - prefix 之前不能是反斜杠（避免吃掉 \theta 的 'eta'）；
    #   - $...$ 内部必须以 \、_ 或 ^ 开头，确保捕获的确实是 LaTeX 表达式——
    #     如果以空格/标点/普通字母开头就是普通英文文本，不是公式。
    #
    # 正确合并：
    #   "H$_{\text{tok}}$"   → "$H_{\text{tok}}$"
    #   "HD$_{\max}$"        → "$HD_{\max}$"
    #   "x$^2$"              → "$x^2$"
    #
    # 拒绝合并（内部不以 \ / _ / ^ 开头，是普通英文）：
    #   "d$ denotes cosine distance. High $H_{\text{sem}}$" → 不匹配 ← 之前写法的 bug
    #   "l$, the hidden state $\mathbf{h}_{l}$"            → 不匹配
    #   "area$_{X}$"        → 不匹配（area 超长）
    text = re.sub(
        r"(?<![\\])\b([A-Za-z0-9_]{1,3})\$([\\_{}^][^$\n]+?)\$",
        r"$\1\2$",
        text,
    )
    # 步骤 1.5：合并紧邻 $...$ 后面的 ↓/↑ Unicode 箭头
    # ar5iv 部分内容把指标下标用 <sub> 渲染（已转为 $_{...}$），
    # 箭头却以纯文本 ↓/↑ 跟在后面，导致 $HD_{max}$↓ 这种"公式+游离箭头"。
    # 把箭头转为 LaTeX 并入公式，让 protect_math 整段保护，避免 LLM 把箭头
    # 当作待译文本从而生成 HD↓max{}_{\max}\downarrow 之类的残骸。
    _ARROW_TEX = {"↓": r"\downarrow", "↑": r"\uparrow"}
    text = re.sub(
        r"\$([^$\n]+?)\$([↑↓])",
        lambda m: f"${m.group(1)}{_ARROW_TEX[m.group(2)]}$",
        text,
    )
    # 步骤 2：补空格，避免中英文混淆
    # CJK 与 $...$ 之间加空格（"使用$T=0.8$个" → "使用 $T=0.8$ 个"）
    text = re.sub(r"([一-鿿])(\$)", r"\1 \2", text)
    text = re.sub(r"(\$)([一-鿿])", r"\1 \2", text)
    return text


def _strip_tag_prefix(text: str) -> str:
    """保留标题原始文本（编号是学术论文有意义的章节引用，如 '3.4. Application'）。"""
    return text.strip()


def parse_arxiv_html(
    html_path: str | Path,
    *,
    img_mapping: dict[str, str] | None = None,
) -> list[Block]:
    """解析 ar5iv HTML，返回结构化 Block 列表（正文顺序，不含参考文献/页脚）。

    img_mapping: 当开启本地图片模式时，传入 原始src -> 本地相对路径 的映射，
                 图片引用会被替换为本地路径；不传则保持原网络 URL。
    """
    html_path = Path(html_path)
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    img_mapping = img_mapping or {}
    # 非本地模式下用此基准把 HTML 里的相对图片路径补全为完整网络 URL。
    # ar5iv 的 <img src> 形如 "2605.26158v1/x1.png"（相对 HTML 文档根，
    # 文档根为 https://arxiv.org/html/<id>/），故基准为 https://arxiv.org/html/。
    base_url = "https://arxiv.org/html/"

    blocks: list[Block] = []

    doc_title = soup.find(class_="ltx_title_document")
    if doc_title:
        blocks.append(Block(kind="title", level=1,
                            text=_plain_text(doc_title).strip(),
                            raw=_plain_text(doc_title).strip()))

    abstract = soup.find(class_="ltx_abstract")
    if abstract:
        abstract_title = abstract.find(class_="ltx_title_abstract")
        if abstract_title:
            blocks.append(Block(kind="heading", level=2,
                                text=_strip_tag_prefix(_plain_text(abstract_title)),
                                raw=_strip_tag_prefix(_plain_text(abstract_title))))
        for p in abstract.find_all(class_="ltx_p", recursive=False):
            rich = _rich_text(p)
            blocks.append(Block(kind="paragraph", text=_plain_text_for_translation(p), raw=rich))
        keywords = abstract.find(class_="ltx_keywords")
        if keywords:
            blocks.append(Block(kind="paragraph", text=_plain_text(keywords),
                                raw="**关键词：** " + _plain_text(keywords).strip()))

    content = soup.find("article") or soup.find(class_="ltx_page_content") or soup.body
    for sec in content.find_all(["section", "subsection", "subsubsection"],
                                class_=re.compile(r"ltx_section"),
                                recursive=False):
        title_tag = sec.find(class_=re.compile(r"ltx_title_section"))
        if title_tag:
            raw_title = _strip_tag_prefix(_plain_text(title_tag))
            blocks.append(Block(kind="heading", level=2, text=raw_title, raw=raw_title))
        _walk_section(sec, blocks, img_mapping, base_url)

    return blocks


def _walk_section(sec: Tag, blocks: list[Block], img_mapping: dict[str, str],
                  base_url: str = "") -> None:
    """按文档顺序遍历 section 的直接子内容节点。"""
    for child in sec.children:
        if not isinstance(child, Tag):
            continue
        cls = child.get("class") or []

        if child.name in ("section", "subsection", "subsubsection") and any(
            c in " ".join(cls) for c in ("ltx_subsection", "ltx_subsubsection")
        ):
            title_tag = child.find(class_=re.compile(r"ltx_title_(subsection|subsubsection)"))
            if title_tag:
                raw_title = _strip_tag_prefix(_plain_text(title_tag))
                level = 3 if "ltx_title_subsection" in (title_tag.get("class") or []) else 4
                blocks.append(Block(kind="heading", level=level, text=raw_title, raw=raw_title))
            _walk_section(child, blocks, img_mapping, base_url)
            continue

        if "ltx_para" in cls:
            for p in child.find_all(class_="ltx_p", recursive=False):
                rich = _rich_text(p)
                if rich:
                    blocks.append(Block(kind="paragraph", text=_plain_text_for_translation(p), raw=rich))
            for eq in child.find_all(class_=re.compile(r"ltx_equation(|group)"), recursive=False):
                _append_equation(eq, blocks)
            continue

        if child.name in ("ul", "ol") and any(
            c in " ".join(cls) for c in ("ltx_itemize", "ltx_enumerate")
        ):
            for li in child.find_all(class_="ltx_item", recursive=False):
                for p in li.find_all(class_="ltx_p", recursive=False):
                    rich = _rich_text(p)
                    if rich:
                        blocks.append(Block(kind="list_item", level=0,
                                            text=_plain_text_for_translation(p), raw="- " + rich))
            continue

        if "ltx_equation" in cls:
            _append_equation(child, blocks)
            continue

        if "ltx_figure" in cls:
            _append_figure(child, blocks, img_mapping, base_url)
            continue

        if "ltx_table" in cls:
            _append_table(child, blocks)
            continue


def _append_equation(eq: Tag, blocks: list[Block]) -> None:
    is_group = "ltx_equationgroup" in (eq.get("class") or [])
    tex = _collect_equationgroup_tex(eq) if is_group else _collect_equation_tex(eq)
    if not tex:
        return
    label = eq.find(class_=re.compile(r"ltx_tag_equation"))
    lbl = _plain_text(label).strip() if label else ""
    # 公式标签 (1)/(2) 紧跟在 $$ 块后（同一"段落"，不插入空行），
    # 否则标记会被 KaTeX/浏览器隔断渲染成独立行
    md = f"$$\n{tex}\n$$\n"
    if lbl:
        md += f"{lbl}\n"
    blocks.append(Block(kind="equation", text="", raw=md, meta={"label": lbl}))


def _append_figure(fig: Tag, blocks: list[Block], img_mapping: dict[str, str],
                   base_url: str = "") -> None:
    img = fig.find("img")
    src = _as_str(img.get("src")) if img else None
    if not src:
        render_src = None
    elif img_mapping:
        # 本地模式：优先用已下载的本地相对路径
        render_src = img_mapping.get(src, src)
    else:
        # 非本地模式：必须补全为完整网络 URL，否则 Markdown 里的相对路径无法加载
        render_src = src if src.startswith("http") else (base_url.rstrip("/") + "/" + src.lstrip("/"))
    caption = fig.find(class_="ltx_caption")
    cap_text = _rich_text(caption) if caption else ""
    # caption 可能是多行（标签 + 描述），合并成单行引用避免 markdown 引用断裂
    cap_text = cap_text.replace("\n", " ").strip()
    blocks.append(Block(kind="figure", text=_plain_text(caption) if caption else "",
                        raw=(f"![figure]({render_src})\n\n" if render_src else "")
                             + (f"> {cap_text}" if cap_text else ""),
                        meta={"src": src, "local_src": render_src, "caption": cap_text}))


def _collect_table_text(tbl: Tag) -> str:
    """将 ar5iv 表格转为 markdown 表格，正确处理 colspan / rowspan 与分组头。
合并单元格：在每行重复上方 cell 文本以保持列对齐（markdown 不支持原生合并）。
分组头：单 cell + colspan >= max_cols 表示分组标识，后续子项行第 1 列复用该文本。
"""
    tabular = tbl.find(class_="ltx_tabular") or tbl
    html_table = tabular if tabular.name == "table" else tabular.find("table")
    if html_table is None:
        return ""

    def _cell_text(td: Tag) -> str:
        text = _rich_text(td).replace("\n", " ").strip()
        text = text.replace("\xa0", " ").strip()
        return text.replace("|", "\\|")

    # 第一遍：解析每行 cells + 标记表头 + 统计 max_cols（取所有非分组行中 colspan=1 cell 数最大值）
    raw_rows: list[tuple[list[tuple[str, int, int]], bool]] = []
    max_cols = 0
    for tr in html_table.find_all("tr"):
        cls = " ".join(tr.get("class") or [])
        is_header = "ltx_thead" in cls or "ltx_row_header" in cls
        row: list[tuple[str, int, int]] = []
        for td in tr.find_all(["td", "th"]):
            text = _cell_text(td)
            cs = max(int(td.get("colspan") or 1), 1)
            rs = max(int(td.get("rowspan") or 1), 1)
            row.append((text, cs, rs))
        if row:
            # 分组行识别：单 cell 且 colspan >= 2 （或者单 cell + 整组后续行用）
            cs_sum = sum(cs for _, cs, _ in row)
            is_group = len(row) == 1 and cs_sum >= 2
            if not is_group and not is_header:
                # 普通数据行：按 colspan=1 cell 数累计最大列数
                cols_here = sum(1 for _, cs, _ in row if cs == 1) + sum(
                    cs for _, cs, _ in row if cs > 1
                )
                max_cols = max(max_cols, cols_here)
            raw_rows.append((row, is_header))

    if not raw_rows:
        return ""

    # 若未能从数据行推断 max_cols，回退用首行 colspan 之和
    if max_cols == 0:
        max_cols = sum(cs for _, cs, _ in raw_rows[0][0])

    # 第二遍：展开为等宽 grid
    col_remaining: list[int] = []   # 每列上方 cell 还占用多少行（rowspan）
    col_text: list[str] = []        # 被占用列应填的文本
    grid: list[list[str]] = []
    header_idx = -1
    current_group_text = ""         # 最近分组头文本（子项行 Condition 列复用）

    def _ensure_col(idx: int) -> None:
        while len(col_remaining) <= idx:
            col_remaining.append(0)
            col_text.append("")

    for ri, (row, is_header) in enumerate(raw_rows):
        out_row: list[str] = []
        col = 0
        cs_sum = sum(cs for _, cs, _ in row)
        is_group = len(row) == 1 and cs_sum >= 2 and not is_header

        if is_group:
            # 分组头行：仅占第 1 列，文本写入第 1 列，后续列在该行填空
            text = row[0][0]
            current_group_text = text
            # 第 1 列上方可能被 rowspan 占用，跳过
            while col < len(col_remaining) and col_remaining[col] > 0:
                out_row.append(col_text[col])
                col_remaining[col] -= 1
                col += 1
            _ensure_col(col)
            out_row.append(text)
            col_text[col] = text
            col_remaining[col] = 0  # 分组头本身不占用后续行（由子项行复用）
            col += 1
            # 其余列填空（保持等宽）
            while col < max_cols:
                _ensure_col(col)
                out_row.append("")
                col += 1
        else:
            for text, cs, rs in row:
                # 先跳过上方 rowspan 占用的列
                while col < len(col_remaining) and col_remaining[col] > 0:
                    out_row.append(col_text[col])
                    col_remaining[col] -= 1
                    col += 1
                # 若当前 col 是 Condition 列（第 0 列）且本行只有 (max_cols-1) 个 cell
                #   且 current_group_text 非空 → 把 current_group_text 注入第 0 列
                if (col == 0 and current_group_text and not is_header
                        and len(row) <= max_cols - 1):
                    # 本行是子项：先放分组头文本在 Condition 列
                    _ensure_col(0)
                    out_row.append(current_group_text)
                    col_text[0] = current_group_text
                    col_remaining[0] = 0
                    col = 1
                    # 重新进入内层：跳过上方占用 + 写本 cell（其实下面立即会处理第一个 cell）
                # 写入当前 cell 起始列
                _ensure_col(col)
                out_row.append(text)
                col_text[col] = text
                col_remaining[col] = rs - 1
                col += 1
                # colspan：右侧 cs-1 列在当前行输出空
                for _ in range(cs - 1):
                    _ensure_col(col)
                    out_row.append("")
                    col_remaining[col] = 1
                    col += 1
            # 行末处理仍有 rowspan 占用的列
            while col < len(col_remaining) and col_remaining[col] > 0:
                out_row.append(col_text[col])
                col_remaining[col] -= 1
                col += 1
            # 补齐到 max_cols
            while col < max_cols:
                out_row.append("")
                col += 1
            # 移除尾部完全空闲的列
            while col_remaining and col_remaining[-1] == 0:
                col_remaining.pop()
                col_text.pop()

        grid.append(out_row)
        if is_header and header_idx == -1:
            header_idx = ri

    if not grid:
        return ""
    max_cols = max(max_cols, max(len(r) for r in grid))
    grid = [r + [""] * (max_cols - len(r)) for r in grid]

    if 0 <= header_idx < len(grid):
        header_row = grid[header_idx]
        body_rows = [r for i, r in enumerate(grid) if i != header_idx]
    else:
        header_row = grid[0]
        body_rows = grid[1:]

    lines = ["| " + " | ".join(header_row) + " |",
             "| " + " | ".join(["---"] * max_cols) + " |"]
    for r in body_rows:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def _append_table(tbl: Tag, blocks: list[Block]) -> None:
    caption = tbl.find(class_="ltx_caption")
    cap_text = _rich_text(caption) if caption else ""
    table_md = _collect_table_text(tbl)
    # raw 同时保留表格结构与 caption，便于翻译与最终输出。
    # 学术规范：表说明（表注）置于表格下方。
    raw_parts = []
    if table_md:
        raw_parts.append(table_md)
    if cap_text:
        raw_parts.append(f"> {cap_text.replace(chr(10), ' ').strip()}")
    raw = "\n\n".join(raw_parts) if raw_parts else "(表格)"
    blocks.append(Block(kind="table", text=(_plain_text(caption) if caption else "") +
                        ("\n" + table_md if table_md else ""),
                        raw=raw,
                        meta={"caption": cap_text, "table_md": table_md}))


def _collect_equation_tex(eq: Tag) -> str:
    """单行 equation：直接返回去除 \\displaystyle 后的 LaTeX。"""
    parts: list[str] = []
    for ann in eq.find_all("annotation", attrs={"encoding": "application/x-tex"}):
        if ann.string:
            parts.append(_strip_displaystyle(ann.string.strip()))
    return " ".join(parts)


def _collect_equationgroup_tex(eq: Tag) -> str:
    """equationgroup（多行对齐公式）：按 row 收集，用 & 拼接各列，\\\\ 换行，包 aligned。"""
    rows: list[str] = []
    for tr in eq.find_all("tr", class_="ltx_equation"):
        cells = []
        for td in tr.find_all("td", class_=re.compile(r"ltx_eqn_cell")):
            # 跳过空白 pad 单元格
            cls = " ".join(td.get("class") or [])
            if "ltx_eqn_center_padleft" in cls or "ltx_eqn_center_padright" in cls:
                continue
            cell_tex: list[str] = []
            for ann in td.find_all("annotation", attrs={"encoding": "application/x-tex"}):
                if ann.string:
                    cell_tex.append(_strip_displaystyle(ann.string.strip()))
            if cell_tex:
                cells.append(" ".join(cell_tex))
        if cells:
            rows.append(" & ".join(cells))
    if not rows:
        return ""
    body = " \\\\ \n".join(rows)
    return r"\begin{aligned}" + "\n" + body + "\n" + r"\end{aligned}"
