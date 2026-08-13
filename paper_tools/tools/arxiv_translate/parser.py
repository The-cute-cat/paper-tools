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


# ar5iv 正文引用 <cite><a href="#bib...">作者 年份</a></cite> 的 <a title> 通常为空，
# 论文题目其实保存在文末 bibliography 区块（#bib.xxx 对应的 <li class="ltx_bibitem">）。
# 正文引用解析时从这里取论文名，才能生成带"搜索引擎链接 + 悬浮论文名"的引用，
# 否则引用只能退化成无链接的纯文字（再经翻译后彻底丢失）。
# 该映射在 parse_arxiv_html 开头按当前 soup 重建，避免跨文件调用串味。
_BIB_MAP: dict[str, str] = {}


def _build_bib_map(soup: "BeautifulSoup") -> dict[str, str]:
    """解析 ar5iv bibliography，建立 #bib.xxx -> 论文标题 映射。

    ar5iv 的 <li class="ltx_bibitem" id="bib.bibN"> 内通常含多个 <span
    class="ltx_bibblock">：第一个多为 "Author. Year."，后续含论文标题与 URL。
    策略：取所有 ltx_bibblock 文本，剔除含 http 的（那是链接块），再从剩余里
    取第一个不含 4 位年份的块作为标题（标题一般形如 "Introducing chatgpt."）。
    """
    bib_map: dict[str, str] = {}
    for item in soup.find_all("li", class_="ltx_bibitem"):
        bid = item.get("id")
        if not isinstance(bid, str) or not bid.startswith("bib."):
            continue
        anchor = "#" + bid
        blocks = [b.get_text(" ", strip=True) for b in item.find_all(class_="ltx_bibblock")]
        blocks = [b for b in blocks if b and "http" not in b.lower()]
        title = ""
        for b in reversed(blocks):
            if not re.search(r"\b(?:19|20)\d{2}\b", b):
                title = b.rstrip(". ").strip()
                break
        if not title and blocks:
            title = blocks[-1].rstrip(". ").strip()
        if title:
            bib_map[anchor] = title
    return bib_map


@dataclass
class Block:
    """一个内容块，对应 markdown 中的一段可翻译单元。"""
    kind: str  # "heading" | "paragraph" | "equation" | "figure" | "table" | "list_item" | "title"
    level: int = 0          # heading 的 markdown 级别
    text: str = ""          # 需要翻译的纯英文文本
    raw: str = ""           # 已含 markdown 标记（公式/链接保留）的文本
    meta: dict = field(default_factory=dict)


def _assign_html_ids(soup: "BeautifulSoup") -> None:
    """给每个可翻译文本容器打 data-zh-id。

    翻译完成后，pipeline 会按 id 找到对应 tag 并替换其文本，
    从而生成保留原 HTML 结构（表格合并/颜色/图片）的 .zh.html。
    """
    _counter = {"n": 0}
    # 需要打标的容器：段落、标题、列表项、表格（整张表一个 id）、
    # 图（figure 容器一个 id，caption 文本会被一起替换）。
    targets = soup.find_all(
        ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "table", "figure", "blockquote"]
    )
    for tag in targets:
        # 跳过已属于某个带 id 父标签的子标签（避免重复打标）
        if tag.find_parent(attrs={"data-zh-id": True}) is not None:
            continue
        _counter["n"] += 1
        tag["data-zh-id"] = f"zh-{_counter['n']}"


def _html_id_of(tag: "Tag") -> Optional[str]:
    """取 tag 自身的 data-zh-id；若无则向上找最近的带 id 祖先。"""
    if tag is None:
        return None
    own = tag.get("data-zh-id")
    if own:
        return own
    parent = tag.find_parent(attrs={"data-zh-id": True})
    if parent is not None:
        return parent.get("data-zh-id")
    return None


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
                    # 正文引用的 <a title> 通常为空，论文名在 bibliography 区块；
                    # 用预解析的 #bib.xxx -> 论文标题 映射补上，确保引用仍能生成
                    # 带"搜索引擎链接 + 悬浮论文名"的可点击引用。
                    if not title:
                        href = _as_str(c.get("href")) or ""
                        title = _BIB_MAP.get(href, "")
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
        # 块级元素边界插入换行，便于表格 cell 等"行内多段"场景保留段落结构。
        # ar5iv Table 1 等对话示例用 <span class="ltx_p"> / <p class="ltx_p">
        # 把多轮 User/Assistant 分段；如果这里不产生换行，所有内容会被吞成
        # 一段空白分隔的长串，导致模型翻译出来的对话丢失结构（参见 issue：
        # ICD 对话示例被压成一行）。下面的 re.sub(r"\n{2,}", "\n") 会把多换
        # 行折叠成单 \n，普通段落调用方不受影响（GFM 段落忽略单换行）。
        _BLOCK_NAMES = {
            "p", "div", "li", "ul", "ol", "tr", "td", "th",
            "blockquote", "pre", "section", "article",
        }
        if name in _BLOCK_NAMES:
            parts.append("\n")
            for c in node.children:
                walk(c)
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
    base_url: str = "https://arxiv.org/html/",
) -> tuple[list[Block], "BeautifulSoup"]:
    """解析 ar5iv HTML，返回 (blocks, soup)。

    - blocks：结构化 Block 列表（正文顺序，不含参考文献/页脚）。
    - soup：已打好 data-zh-id 标注的 BeautifulSoup 对象，供翻译完成后回写
            译文、生成保留原 HTML 结构（表格合并/颜色/图片）的 .zh.html。

    img_mapping: 当开启本地图片模式时，传入 原始src -> 本地相对路径 的映射，
                 图片引用会被替换为本地路径；不传则保持原网络 URL。
    base_url: HTML 文档根 URL（可能含版本号，如
              https://arxiv.org/html/2603.16192v1/），用于把相对图片路径
              补全为完整网络 URL。默认 https://arxiv.org/html/。
    """
    html_path = Path(html_path)
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    img_mapping = img_mapping or {}
    # 非本地模式下用此基准把 HTML 里的相对图片路径补全为完整网络 URL。
    # ar5iv 的 <img src> 形如 "2605.26158v1/x1.png"（相对 HTML 文档根）。

    blocks: list[Block] = []

    # 给每个"可翻译文本容器"打一个稳定的 data-zh-id，供翻译后回写 HTML
    # （生成 .zh.html，保留原 HTML 的表格合并/颜色/图片结构）。
    _assign_html_ids(soup)

    # 预解析 bibliography，建立 #bib.xxx -> 论文标题 映射，供正文引用生成链接用。
    global _BIB_MAP
    _BIB_MAP = _build_bib_map(soup)

    # 本地图片模式：把 <img src> / <object data> 改写为本地相对路径，
    # 这样生成的 .zh.html 可被 DOCX/PDF 导出器正确内嵌图片。
    if img_mapping:
        for img in soup.find_all("img"):
            src = img.get("src")
            if isinstance(src, str) and src in img_mapping:
                img["src"] = img_mapping[src]
        for obj in soup.find_all("object"):
            data = obj.get("data")
            if isinstance(data, str) and data in img_mapping:
                obj["data"] = img_mapping[data]

    doc_title = soup.find(class_="ltx_title_document")
    if doc_title:
        blocks.append(Block(kind="title", level=1,
                            text=_plain_text(doc_title).strip(),
                            raw=_plain_text(doc_title).strip(),
                            meta={"html_id": _html_id_of(doc_title)}))

    abstract = soup.find(class_="ltx_abstract")
    if abstract:
        abstract_title = abstract.find(class_="ltx_title_abstract")
        if abstract_title:
            blocks.append(Block(kind="heading", level=2,
                                text=_strip_tag_prefix(_plain_text(abstract_title)),
                                raw=_strip_tag_prefix(_plain_text(abstract_title)),
                                meta={"html_id": _html_id_of(abstract_title)}))
        for p in abstract.find_all(class_="ltx_p", recursive=False):
            rich = _rich_text(p)
            blocks.append(Block(kind="paragraph", text=_plain_text_for_translation(p), raw=rich,
                                meta={"html_id": _html_id_of(p)}))
        keywords = abstract.find(class_="ltx_keywords")
        if keywords:
            blocks.append(Block(kind="paragraph", text=_plain_text(keywords),
                                raw="**关键词：** " + _plain_text(keywords).strip(),
                                meta={"html_id": _html_id_of(keywords)}))

    content = soup.find("article") or soup.find(class_="ltx_page_content") or soup.body
    for sec in content.find_all(["section", "subsection", "subsubsection"],
                                class_=re.compile(r"ltx_section"),
                                recursive=False):
        title_tag = sec.find(class_=re.compile(r"ltx_title_section"))
        if title_tag:
            raw_title = _strip_tag_prefix(_plain_text(title_tag))
            blocks.append(Block(kind="heading", level=2, text=raw_title, raw=raw_title,
                                meta={"html_id": _html_id_of(title_tag)}))
        _walk_section(sec, blocks, img_mapping, base_url)

    return blocks, soup


def _emit_list(ul: Tag, blocks: list[Block]) -> None:
    """把一个 <ul>/<ol> 列表切成若干 list_item Block。

    ar5iv 列表项（<li class="ltx_item">）内部有两种结构：
      1) <li><p class="ltx_p">...</p></li>                  （扁平，p 是 li 直接子节点）
      2) <li><div class="ltx_para"><p class="ltx_p">...</p></div></li>
                                                                 （p 嵌在 div.ltx_para 里一层）
    因此查找 <p> 必须用 recursive=True，否则第 2 种结构下 li 直接子节点只有
    <span class="ltx_tag_item"> 和 <div>，<p> 在 div 内 → 列表项被整体漏掉。
    """
    if not any(c in " ".join(ul.get("class") or []) for c in ("ltx_itemize", "ltx_enumerate")):
        return
    for li in ul.find_all(class_="ltx_item", recursive=False):
        for p in li.find_all(class_="ltx_p", recursive=True):
            rich = _rich_text(p)
            if rich:
                blocks.append(Block(kind="list_item", level=0,
                                    text=_plain_text_for_translation(p), raw="- " + rich,
                                    meta={"html_id": _html_id_of(p)}))


def _emit_text_box(span: Tag, blocks: list[Block]) -> None:
    """处理 ar5iv 的"图片化文本框"（带边框的灰色提示框，通常是 Prompt 示例、
    调用样例等可视化代码块）。

    ar5iv 把这类文本框直接渲染成一张 SVG 图片（<span class="ltx_inline-block">
    包 <svg class="ltx_picture"> + <foreignobject>），但 SVG 内部仍然保留
    真实段落（<span class="ltx_p">），可结构化提取。早期实现只识别
    <p>/<table>/<ul>/<ol>，对这种 span 容器直接 continue，导致整段内容丢失。

    策略：把 span 内的所有 <span class="ltx_p"> 段落**合并成一个** Block（类型
    ``text_box``），原始 markdown 用 ``> `` 引用块逐行包裹，保留"框"的视觉语义。
    合并的多段一起翻译，确保模型/手动翻译时能保留段落间上下文衔接。
    """
    # 防御：必须是 ltx_inline-block；非则不处理
    if "ltx_inline-block" not in " ".join(span.get("class") or []):
        return

    # 找出"实在的"段落：<span class="ltx_p"> 内嵌在 foreignobject 之下。
    paragraphs = span.find_all(class_="ltx_p", recursive=True)
    real_ps: list[Tag] = []
    for p in paragraphs:
        if not _rich_text(p).strip():
            continue
        real_ps.append(p)
    if not real_ps:
        return

    # 合并所有段落：原文按 "\n" 拼接（保持多段结构），富文本 raw 用 "> " 逐段包裹
    plain_parts: list[str] = []
    rich_parts: list[str] = []
    for p in real_ps:
        rich = _rich_text(p)
        plain = _plain_text_for_translation(p)
        if not rich.strip():
            continue
        plain_parts.append(plain)
        rich_parts.append(f"> {rich}")

    if not rich_parts:
        return

    # 主动给这个 text_box 容器打一个 data-zh-id，供后续 .zh.html 回填时定位。
    # 它内嵌在 SVG <foreignobject> 里，不会被子流程 _assign_html_ids 自动打标。
    # 用块序号生成稳定 id（soup 在 parse 阶段已经过 _assign_html_ids，
    # 这里追加的新 id 不会与已有冲突，因为已用的 id 是 zh-N 形式）。
    box_id = f"zh-textbox-{len(blocks)}"
    span["data-zh-id"] = box_id

    blocks.append(Block(
        kind="text_box",
        text="\n".join(plain_parts),
        raw="\n".join(rich_parts),
        meta={"html_id": box_id,
              "text_box_md": "\n".join(rich_parts)},
    ))


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
                blocks.append(Block(kind="heading", level=level, text=raw_title, raw=raw_title,
                                    meta={"html_id": _html_id_of(title_tag)}))
            _walk_section(child, blocks, img_mapping, base_url)
            continue

        if "ltx_para" in cls:
            # ltx_para 内部通常交错着 <p class="ltx_p"> 段落和 <table class="ltx_equation...">
            # 公式块。早期实现是"先按段落、再按公式"两轮 append，会把整个段落容器
            # 内所有段落挤在前面、所有公式挤在后面，丢失原文的"段-公式-段-公式"交错
            # 顺序（例如 3.4 节：P1, Eq1, Eq2, P2, Eq3, P4, Eq4 会被展平成
            #       P1, P2, P4, Eq1, Eq2, Eq3, Eq4）。
            # 修复：按子节点文档顺序遍历，逐节点分发到 paragraph / equation append，
            #       保留原始位置关系。
            for sub in child.find_all(["p", "table", "ul", "ol", "span"], recursive=False):
                sub_cls = sub.get("class") or []
                if "ltx_p" in sub_cls:
                    rich = _rich_text(sub)
                    if rich:
                        blocks.append(Block(
                            kind="paragraph",
                            text=_plain_text_for_translation(sub),
                            raw=rich,
                            meta={"html_id": _html_id_of(sub)},
                        ))
                elif "ltx_equation" in sub_cls or "ltx_equationgroup" in sub_cls:
                    _append_equation(sub, blocks)
                elif sub.name in ("ul", "ol"):
                    # ltx_para 内可能直接嵌套列表（这是 ar5iv 把整个小节包进
                    # 一个 ltx_para 时的常见结构，例如 4.2 Datasets 节：
                    # <div class="ltx_para"><ul class="ltx_itemize">...</ul></div>）。
                    # 早期实现漏掉了这种情况，导致列表项整体丢失。
                    _emit_list(sub, blocks)
                elif "ltx_inline-block" in sub_cls:
                    # ar5iv 把"Prompt 示例"等带框内容渲染成 SVG 图片
                    # （<span class="ltx_inline-block"><svg class="ltx_picture">...
                    # <foreignobject><span class="ltx_p">真实文本</span></foreignobject>）。
                    # 实际文本可回收利用，必须在 ltx_para 分支里递归挖出来。
                    _emit_text_box(sub, blocks)
            continue

        # ar5iv 实际生成的段落容器是 <section class="ltx_paragraph">，与上文
        # ltx_para（div 容器）不同；这里用子串匹配兼容 ltx_paragraph，
        # 并收集挂载在其内部的 figure/table/listing。
        if "ltx_paragraph" in " ".join(cls):
            # 段落小标题（如 <h4 class="ltx_title_paragraph">）—— 这些是结构性子节点，
            # 不参与下面的"按文档顺序混合 paragraph/equation"传递。
            for h4 in child.find_all(class_="ltx_title_paragraph", recursive=False):
                raw_title = _strip_tag_prefix(_plain_text(h4))
                if raw_title:
                    blocks.append(Block(kind="heading", level=4, text=raw_title, raw=raw_title,
                                        meta={"html_id": _html_id_of(h4)}))

            # ltx_paragraph 内可能嵌套 <div class="ltx_para"> 而 <div> 内再嵌
            # <p class="ltx_p">；而公式块（<table class="ltx_equation...">）、
            # figure、table、listing 通常直接挂在 ltx_paragraph 下。
            # 行为约束：paragraph / equation 必须保持文档顺序；
            # figure / table / listing 是块级结构元素也参与顺序维护。
            TAG_KINDS = ("h4", "div", "p", "table", "figure")

            def _emit(sub):
                sub_cls = sub.get("class") or []
                sub_cls_set = set(sub_cls)
                if "ltx_p" in sub_cls_set:
                    rich = _rich_text(sub)
                    if rich:
                        blocks.append(Block(
                            kind="paragraph",
                            text=_plain_text_for_translation(sub),
                            raw=rich,
                            meta={"html_id": _html_id_of(sub)},
                        ))
                    return
                if "ltx_title_paragraph" in sub_cls_set:
                    # 已在上方统一处理；此处忽略，避免重复。
                    return
                if "ltx_equation" in sub_cls_set or "ltx_equationgroup" in sub_cls_set:
                    _append_equation(sub, blocks)
                    return
                if "ltx_figure" in sub_cls_set:
                    _append_figure(sub, blocks, img_mapping, base_url)
                    return
                if "ltx_table" in sub_cls_set:
                    _append_table(sub, blocks)
                    return
                if "ltx_algorithm" in sub_cls_set or "ltx_listing" in sub_cls_set:
                    _append_listing(sub, blocks)
                    return
                if sub.name == "div" and "ltx_para" in sub_cls_set:
                    # 嵌套段落容器：递归把它的直接 paragraph / equation
                    # 子元素追加到外层 block 列表（保持与外层 figure/table/listing
                    # 在同一文档顺序里）。
                    for inner_p in sub.find_all(class_="ltx_p", recursive=False):
                        _emit(inner_p)
                    for inner_eq in sub.find_all(
                        class_=re.compile(r"ltx_equation(|group)"),
                        recursive=False,
                    ):
                        _emit(inner_eq)
                    return
                # 其他标签（maketitle 残余等）：不展开，避免污染。

            for sub in child.find_all(TAG_KINDS, recursive=False):
                _emit(sub)
            continue

        if child.name in ("ul", "ol") and any(
            c in " ".join(cls) for c in ("ltx_itemize", "ltx_enumerate")
        ):
            _emit_list(child, blocks)
            continue

        if "ltx_equation" in cls:
            _append_equation(child, blocks, html_id=_html_id_of(child))
            continue

        if "ltx_figure" in cls:
            _append_figure(child, blocks, img_mapping, base_url)
            continue

        if "ltx_table" in cls:
            _append_table(child, blocks)
            continue

        # 伪代码 / 算法块（ltx_algorithm + ltx_listing）
        if "ltx_algorithm" in cls or "ltx_listing" in cls:
            _append_listing(child, blocks)
            continue


def _append_equation(eq: Tag, blocks: list[Block], html_id: Optional[str] = None) -> None:
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
    blocks.append(Block(kind="equation", text="", raw=md,
                        meta={"label": lbl, "html_id": html_id} if html_id else {"label": lbl}))


def _append_figure(fig: Tag, blocks: list[Block], img_mapping: dict[str, str],
                   base_url: str = "") -> None:
    # ar5iv 对较大/矢量图（如阈值敏感性分析的 SVG）使用 <object data="*.svg"> 嵌入，
    # 而不是 <img src="*.png">。<object> 在浏览器中渲染正常但不在 fig.find("img") 范围内，
    # 必须同时兼容这两种资源嵌入方式，否则会产生"caption 在但图缺失"的现象。
    src: Optional[str] = None
    img = fig.find("img")
    if img is not None:
        s = _as_str(img.get("src"))
        if s:
            src = s
    if not src:
        obj = fig.find("object")
        if obj is not None:
            s = _as_str(obj.get("data"))
            if s:
                src = s
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
                        meta={"src": src, "local_src": render_src, "caption": cap_text,
                              "html_id": _html_id_of(fig)}))


def _append_listing(fig: Tag, blocks: list[Block]) -> None:
    """解析 ar5iv 伪代码/算法块（ltx_algorithm / ltx_listing）。

    ltx_listing 中每行是一个 ltx_listingline，含行号标签、正文（含 inline math）
    ／关键字加粗等。caption 走 LLM 翻译，代码内容本身保留英文不翻译（因其由
    公式/变量名/伪代码关键字组成，翻译毫无意义且会破坏语义）。

    输出为 **两列 GFM 表格**（列1 = 行号标签，列2 = 代码内容），而非 fenced code
    block —— 这样 Typora 等渲染器能在代码内容中正常解析 inline math（$…$），
    同时行号与代码左右对齐、可读性好。代码内容中的 `|` 会转义避免破坏表格结构。
    caption 置于表格下方（学术规范：表注/算法说明在块之后）。
    """
    # --- caption ---
    caption = fig.find("figcaption") or fig.find(class_="ltx_caption")
    cap_text = _rich_text(caption) if caption else ""
    # 提取纯文本供翻译（类似 _append_table 只传 caption 给 translator）
    cap_plain = _plain_text(caption).strip() if caption else ""

    # --- listing content ---
    listing = fig.find(class_="ltx_listing")
    if listing is None:
        listing = fig  # 可能是裸 ltx_listing div（非 figure 包裹）

    rows: list[tuple[str, str]] = []  # (label, content)
    for line_div in listing.find_all(class_="ltx_listingline", recursive=False):
        # 行号标签
        tag = line_div.find(class_="ltx_tag_listingline")
        line_no = ""
        if tag:
            line_no = tag.get_text().strip()
            tag.decompose()
        for br in line_div.find_all("br"):
            br.decompose()
        content = _listing_line_text(line_div)
        # 单元格内不允许换行：多个 listingline 内嵌的 <br>/多行 math（如 cases）
        # 会以 \n 出现，统一替换为空格（LaTeX cases 内的 \\ 换行已在前置处理为
        # 字面 \\，保留语义）。同时压缩多余空白。
        content = content.replace("\n", " ").strip()
        content = re.sub(r"[ \t]{2,}", " ", content)
        if not content and not line_no:
            continue
        if line_no:
            rows.append((line_no, content))
        else:
            # 无行号行：Data: / Result: 等声明行，用自身前缀作标签
            # 尝试识别 "Word:" 形式
            m = re.match(r"^([A-Za-z][A-Za-z ]*?):\s*(.*)$", content)
            if m and len(m.group(1)) <= 12:
                rows.append((m.group(1), m.group(2)))
            else:
                rows.append(("", content))

    # 组装两列表格。GFM 表格强制要求 header row，否则不识别为表格。
    # 使用 zero-width space (U+200B) 作占位单元格 —— 视觉上完全不可见，
    # 但满足 GFM "单元格内必须有内容"的语法要求，避免渲染成两行空白。
    # 分隔行用 :-: 对齐让它视觉上看起来"中心对齐"占位。
    _ZW = "\u200b"
    table_lines: list[str] = [f"{_ZW} | {_ZW}", ":-: | :-"]
    for label, content in rows:
        # 单元格内 | 转义，避免破坏表格
        cell_content = content.replace("|", "\\|").strip()
        if cell_content == "":
            table_lines.append(f"| {label} | |")
        else:
            table_lines.append(f"| {label} | {cell_content} |")
    code_block = "\n".join(table_lines)

    # --- caption 翻译交给 translator，代码块原样保留 ---
    raw_parts: list[str] = [code_block]
    if cap_text:
        cap_text_clean = cap_text.replace("\n", " ").strip()
        raw_parts.append(f"> {cap_text_clean}")
    raw = "\n\n".join(raw_parts) if raw_parts else "(伪代码)"

    caption_tag = fig.find("figcaption") or fig.find(class_="ltx_caption")
    blocks.append(Block(
        kind="listing",  # 独立类型，与 figure/table 区分
        text=cap_plain,
        raw=raw,
        meta={"caption": cap_text, "listing_md": code_block, "listing_raw": raw,
              "html_id": _html_id_of(fig)},
    ))


def _listing_line_text(tag: Tag) -> str:
    """将 listing 行内容转为保留 inline 数学公式的纯文本（不用 _rich_text 以避免 ** 等标记）。"""
    parts: list[str] = []

    def walk(node):
        if isinstance(node, NavigableString):
            parts.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        cls = node.get("class") or []
        # 匹配 ltx_Math、ltx_math_unparsed 以及裸 <math> 标签
        if node.name == "math" or "ltx_Math" in cls or "ltx_math" in cls:
            tex = _tex_of(node)
            if tex:
                parts.append(f"${tex}$")
            return
        if node.name in ("sub", "sup"):
            inner = node.get_text().strip()
            if inner:
                sym = "_" if node.name == "sub" else "^"
                parts.append(f"${sym}{{{inner}}}$")
            return
        # 忽略行号标签和 <br>
        if "ltx_tag_listingline" in cls or node.name == "br":
            return
        for c in node.children:
            walk(c)

    walk(tag)
    text = "".join(parts)
    # 压缩多余空白（代码块中不需要中英文间距调整）
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


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
        rich = _rich_text(td)
        # 把行内换行折叠成单个 "\n"（_rich_text 已把相邻 \n 压成单个），
        # 然后再按行去除首尾空白后用 "<br>" 拼接 —— 这样表格 cell 里的
        # 多段对话（ar5iv 常用 <span class="ltx_p"> 模拟多行）能保留可读
        # 结构，不会被压成一行。原实现直接 replace("\n"," ") 会把同一
        # 个 cell 的所有对话挤成一段乱序文本，对 LLM 输入和最终渲染都不利。
        lines = [ln.strip().replace("\xa0", " ") for ln in rich.split("\n")]
        # 过滤纯空行（不影响相邻 <br>）
        text = "<br>".join(ln for ln in lines if ln)
        text = text.replace("|", "\\|")
        # 规范化：$...$ 与 **..** 之间必须有空格分隔，否则部分渲染器
        # （如 Typora / 部分 markdown → HTML 转换器）会把紧贴的 ** 误判为
        # 数学公式内部符号（特别是 KaTeX/MathJax 的“^”或“…”)，导致列被吞掉。
        # 同时压缩内部多余空格。
        text = re.sub(r"\$([^$\n|]+?)\$\*\*", r"$ \1$ **", text)
        text = re.sub(r"\*\*\$([^$\n|]+?)\$", r"** $ \1$", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        # 把已配对的 **...** 转成 <strong>...</strong>（HTML bold）。
        # 这样在双层 header 用 <br> 拼接 cell 时，markdown ** 不会跨 <br>
        # 错误配对（GFM 表格 cell 完全支持 HTML 标签，且 KaTeX auto-render
        # 会扫描 <strong> 内部文本识别 $...$ 数学）。
        text = re.sub(r"\*\*([^*\n|]+?)\*\*", r"<strong>\1</strong>", text)
        return text

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

    # 启发式识别"双层表头"：当首 row 被显式标 header，但后续行具有以下特征
    # 时也视为 header（多层 LLM 名称 + 指标头是 ar5iv 常见结构）：
    #   1. 单元格数 + 行内 colspan padding 之和不超过 max_cols（说明还在用
    #      上一行的 colspan 锁住的列）；
    #   2. 不含明显的数据特征：典型 method 名（短缩写如 GCG/PRP 等）或数字；
    #   3. 不含 `<td rowspan=大型>` 的长并行 cell。
    # 数据特征 = 短全大写字母缩写 + 数字 + '-' + 中等长度单 cell（method）
    _SHORT_ABBR = re.compile(r"^[A-Z]{2,5}$")
    _SHORT_NUM_DASH = re.compile(r"^[0-9. -]+$")

    # ar5iv 大量表格根本没有 ltx_thead / ltx_row_header class，需要根据内容
    # 启发识别：若首行包含 "Dataset" / "Method" 这种常见表头标签，则视为 header。
    # 注意：表格 label 通常被 `**` 加粗（parser 转后变成 `**Dataset**`），需
    # 兼容 markdown bold 标记（标签前后的 `**` 也允许存在）。
    first_header_label = re.compile(
        r"^\s*\**\s*(Dataset|Method|Symbol|Description|"
        r"Type|Category|Task|Setting|Model|"
        r"Configuration|Component|Step|"
        r"Hyper-?parameter|Layer|Methods?|Models?|Datasets?|Threat\s*Model|"
        r"Defense\s*Strategy|Attack\s*Type|Target(?:ed)?\s*(?:LLM|Model)?|"
        r"Defense|Attack|Hyperparameter)\s*\**\s*$",
        re.IGNORECASE,
    )
    # 允许标签在 `**...**` 加粗内部（被 `_rich_text` 加的 markdown bold）。
    # 用更宽松的正则匹配"cell 内含 Dataset/Method 关键字且非纯数字/单位"。
    first_header_label_loose = re.compile(
        r"\b(Dataset|Method|Symbol|Description|Type|Category|Task|"
        r"Setting|Model|Configuration|Component|Step|Hyper-?parameter|"
        r"Layer|Methods?|Models?|Datasets?|Threat\s*Model|"
        r"Defense\s*Strategy|Attack\s*Type|Target(?:ed)?\s*(?:LLM|Model)?|"
        r"Defense|Attack|Hyperparameter)\b",
        re.IGNORECASE,
    )
    if raw_rows and not raw_rows[0][1]:
        # 至少在首 row 含 "Dataset/Method" 等标签时把它视为 header
        if any(first_header_label.match(t) for t in (text for text, _, _ in raw_rows[0][0])):
            r0 = raw_rows[0][0]
            raw_rows[0] = (r0, True)
        elif any(first_header_label_loose.search(t) and len(t.strip()) <= 60
                  for t in (text for text, _, _ in raw_rows[0][0])):
            # 加粗形式 `**Dataset**` 也能匹配（前后有 ** 也能进）
            r0 = raw_rows[0][0]
            raw_rows[0] = (r0, True)

    last_header_row = -1
    if raw_rows and raw_rows[0][1]:
        last_header_row = 0
        for ri in range(1, len(raw_rows)):
            row, _ = raw_rows[ri]
            cols_span = sum(cs for _, cs, _ in row)
            cell_count = len(row)
            max_rs = max((rs for _, _, rs in row), default=1)
            # 双层 header 检测：单列 / 短 col 的 row 不是双层 header；
            # 必须 cell 数明显多（>2 且 ≥ max_cols 的一半），且列跨度
            # 不超过首层（因 colspan padding 来自上一层）。
            # 进一步：双层 header rows 的单元格内容通常较短、不含数字
            # 单位（"s" "ms" 等除外）和时间戳/括号比例。
            def _is_metric_data(t: str) -> bool:
                # 含时间后缀（"45.5s"）、括号比例（"(+16.3%)"）或纯数字主导 cell → 数据。
                text = t.strip()
                if not text:
                    return False
                # 纯数字 / 数字+括号比例 → 数据（如 "1.0" "45.5s" "52.9s (+16.3%)"）
                if re.match(r"^\d[\d.,()+\-*\s]*$", text):
                    return True
                if re.search(r"\(\+\d", text):
                    return True
                return False

            looks_like_header = (
                max_cols >= 4                     # 表格至少 4 列（避免窄表误识）
                and cell_count >= 3               # 至少 3 个 cell
                and cell_count >= max_cols // 2   # 至少要有一半 cell
                and cols_span <= max_cols + 1     # 且不能超过首层
                and max_rs <= 2
                and not any(_SHORT_ABBR.match(t) and t.isalpha() and t.upper() == t and len(t) <= 5
                            and t not in ("DATASET", "METHOD", "TABLE", "ABBR")
                            for t in (text for text, _, _ in row))
                and not any(_is_metric_data(t)
                            for t in (text for text, _, _ in row))
            )
            if not looks_like_header:
                break
            last_header_row = ri
        if last_header_row > 0:
            raw_rows = [(r, True if i <= last_header_row else h)
                        for i, (r, h) in enumerate(raw_rows)]

    if not raw_rows:
        return ""

    # 若未能从数据行推断 max_cols，回退用首行 colspan 之和
    if max_cols == 0:
        max_cols = sum(cs for _, cs, _ in raw_rows[0][0])

    # 第二遍：展开为等宽 grid
    col_remaining: list[int] = []   # 每列上方 cell 还占用多少行（rowspan）
    col_text: list[str] = []        # 被占用列应填的文本
    col_origin_header: list[bool] = []  # 每列占用 cell 是否 header（True=占位列
                                        # 在被后续 header row 占据时应为空）
    grid: list[list[str]] = []
    header_idx = -1
    current_group_text = ""         # 最近分组头文本（子项行 Condition 列复用）

    def _ensure_col(idx: int) -> None:
        while len(col_remaining) <= idx:
            col_remaining.append(0)
            col_text.append("")
            col_origin_header.append(False)

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
            col_origin_header[col] = is_header
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
                    # 若占位的来源是 header（双层 header 中第 1 层 rowspan 占据
                    # 第 2 层），输出空而不是 col_text——避免 header 行重复。
                    if is_header and col_origin_header[col]:
                        out_row.append("")
                    else:
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
                    col_origin_header[0] = is_header
                    col = 1
                    # 重新进入内层：跳过上方占用 + 写本 cell（其实下面立即会处理第一个 cell）
                # 写入当前 cell 起始列
                _ensure_col(col)
                out_row.append(text)
                col_text[col] = text
                col_remaining[col] = rs - 1
                col_origin_header[col] = is_header
                col += 1
                # colspan：右侧 cs-1 列在当前行输出空（不属于 rowspan 占用）
                for _ in range(cs - 1):
                    _ensure_col(col)
                    out_row.append("")
                    # colspan 占位符不应锁住下方行——把 col_remaining[col] 设为 0
                    # 避免让后续行误以为该列被 rowspan 占用而写出空字符串。
                    col_remaining[col] = 0
                    col_origin_header[col] = is_header
                    col += 1
            # 行末处理仍有 rowspan 占用的列
            while col < len(col_remaining) and col_remaining[col] > 0:
                if is_header and col_origin_header[col]:
                    out_row.append("")
                else:
                    out_row.append(col_text[col])
                col_remaining[col] -= 1
                col += 1
            # 补齐到 max_cols
            while col < max_cols:
                out_row.append("")
                col += 1
            # 移除尾部完全空闲的列（仅清理不在 max_cols 之内的尾部空闲，
            # 避免把 rowspan 占用的合法列误删——这些列 col_remaining[col]>0
            # 但 col_text[col] 在被 pop 后下一行读取会 IndexError 或丢内容）。
            # 安全规则：只有 col >= max_cols 范围内的尾部空闲列才能 pop，
            # 同时被 rowspan 锁住的列（col_remaining[col] > 0）永远不能 pop。
            while len(col_remaining) > max_cols and col_remaining[-1] == 0:
                col_remaining.pop()
                col_text.pop()

        grid.append(out_row)
        if is_header and header_idx == -1:
            header_idx = ri

    if not grid:
        return ""
    max_cols = max(max_cols, max(len(r) for r in grid))
    grid = [r + [""] * (max_cols - len(r)) for r in grid]

    # 收集所有被识别为 header 的 rows；非 header rows 才是 body row。
    # 这样双层 header（如 ar5iv Table II）只输出一次 header（已经"合并"
    # 在 grid 的多 row 中间——下面会把多 header row 折叠为单格的多 row
    # markdown 写法）。
    header_rows: list[int] = []
    body_rows_idx: list[int] = []
    # 重读 raw_rows 的 is_header 状态用于 grid 行筛选
    for i, (row, is_h) in enumerate(raw_rows[: len(grid)]):
        if is_h:
            header_rows.append(i)
        else:
            body_rows_idx.append(i)

    # 如果有双层 header，把它折叠为单层 header（把 LLM 名 + 指标合并）
    # 做法：对每个列，逐行扫描 header，把 rowspan 占位的空 cell 改为
    # "Llama-3 / ASR_L (%)" 这种合并显示形式不可行（GFM 表格不合并），
    # 所以采用更简单的方法：直接丢弃"次层" header（指标行），并把主 header
    # 的 colspan=2 内容作为分组的语义保留在原位。
    #
    # 因为 markdown 表格不支持嵌套单元格，去掉次层 header 会让指标（ASR_L/QS）
    # 消失——所以这里采用**保留全部 header rows**但合并成一个 header row：
    # 对每个列，从上到下选取**最后一个非空**值（这样深层指标的渲染不会被
    # 顶层 LLM 名覆盖）。
    body_rows = [grid[i] for i in body_rows_idx]

    if len(header_rows) >= 2:
        # 多 header row：把每列的多层 cell 内容用 <br> 拼接为一个 cell。
        # 这样既保留了两层 header 信息（外层 LLM 名 + 内层指标）又不破坏 GFM
        # 单行 header 的规则。Typora 会渲染 <br> 为软换行，视觉上模拟 colspan。
        merged_header: list[str] = []
        ncols = max(len(grid[r]) for r in header_rows)
        for c in range(ncols):
            pieces = []
            for r in header_rows:
                row = grid[r]
                if c < len(row) and row[c].strip():
                    pieces.append(row[c].strip())
            # 拼接方式：如果只有一个非空，直接用；否则 <br> 堆叠
            if not pieces:
                merged_header.append("")
            elif len(pieces) == 1:
                merged_header.append(pieces[0])
            else:
                # 用 <br> 拼接（GFM 表格 cell 内允许 <br>）
                merged_header.append("<br>".join(pieces))
        header_row = merged_header
    elif len(header_rows) == 1:
        header_row = grid[header_rows[0]]
    else:
        # 没识别出 header（fallback）
        if grid:
            header_row = grid[0]
            body_rows = grid[1:]
        else:
            return ""

    lines = ["| " + " | ".join(header_row) + " |",
             "| " + " | ".join(["---"] * max_cols) + " |"]
    for r in body_rows:
        lines.append("| " + " | ".join(r) + " |")

    # 丢弃"全为空"的尾列：ar5iv 把 LaTeX 的右 border 装饰 td 渲染成空的尾
    # `<td></td>`（用于在 PDF 里给表格画线），不带任何 cell 文本。如果整张表的
    # 头/体每一行该列都是空字符串，则在 markdown 里就是一个纯空 cell 列，
    # 多余且对渲染毫无意义。这里智能剔除，最右优先（PDF 装饰 td 在尾）。
    # 安全：只在至少保留 2 列时剔除（避免单/双列表被删光，破坏数据）。
    if max_cols >= 3:
        def _col_is_all_empty(c: int) -> bool:
            if c < len(header_row) and header_row[c].strip():
                return False
            for r in body_rows:
                if c < len(r) and r[c].strip():
                    return False
            return True

        # 从右往左扫描（典型 ar5iv 装饰尾列在尾部），可连续删除多个全空尾列
        while len(header_row) > 2 and _col_is_all_empty(len(header_row) - 1):
            cur = len(header_row) - 1
            header_row = header_row[:cur]
            body_rows = [r[:cur] for r in body_rows]
        # 同步重建 lines
        lines = ["| " + " | ".join(header_row) + " |",
                 "| " + " | ".join(["---"] * len(header_row)) + " |"]
        lines.extend("| " + " | ".join(r) + " |" for r in body_rows)

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
                        meta={"caption": cap_text, "table_md": table_md,
                              "html_id": _html_id_of(tbl)}))


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
