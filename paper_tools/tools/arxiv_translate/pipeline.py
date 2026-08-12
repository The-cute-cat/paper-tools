"""arxiv 论文翻译流水线：解析链接 -> 下载 -> 解析 -> 翻译 -> 写出 markdown。"""

import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Optional

from bs4 import BeautifulSoup

from ...config import get_settings
from ...core.downloader import download_binary, download_text
from ...core.glossary import Glossary, KEEP_AS_IS, Term, WRONG_VARIANT_MAP
from ...core.translator import LLMTranslator
from ...logging_setup import get_logger
from .parser import Block, parse_arxiv_html

logger = get_logger()

ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5}(v\d+)?)", re.IGNORECASE)


def _new_tqdm(
    iterable_or_n: Iterable | int,
    desc: str,
    unit: str,
    total: int | None = None,
):
    """统一构造 tqdm：与 logging 同一 stdout 流，避免初始帧残影。

    关键修复：tqdm 必须和 logging 写**同一个 stream（stdout）**。
    若分开写（tqdm→stderr / logging→stdout），在 JetBrains Run 面板、
    Windows console 等会把两个 stream 按到达顺序合并显示的终端里，
    tqdm 的初始帧（如 "翻译表格: 0% 0/7"）先落到 stderr，随后 logging 的
    INFO 行落到 stdout，两行被隔开后，tqdm 后续用 \\r 原地更新的帧再也
    "够不到" 初始帧去擦除它，导致初始帧被永久保留成一条"凭空多出的日志"。

    改为同一 stdout 流后，tqdm 内部用 \\r 在同一行原地刷新，初始帧会被
    后续百分比帧覆盖，结束时 leave=False 清行，无残影。

    支持 `total=` 显式指定进度条总量，给 as_completed 等没有 len() 的迭代器用。
    """
    from tqdm import tqdm
    return tqdm(
        iterable_or_n,
        desc=desc,
        unit=unit,
        total=total,
        file=sys.stdout,         # 与 logging（stdout）同流，\\r 才能正确擦除初始帧
        dynamic_ncols=True,      # 跟随终端宽度
        leave=False,             # 结束后不留残影
        mininterval=0.5,         # 避免与日志刷新竞争
        maxinterval=2.0,
        smoothing=0.1,
    )


def parse_arxiv_id(url_or_id: str) -> str:
    m = ARXIV_ID_RE.search(url_or_id)
    if not m:
        raise ValueError(f"无法从输入中识别 arxiv ID: {url_or_id}")
    return m.group(1)


# 通用学术章节标题映射（领域中立，覆盖绝大多数论文的章节名）。
# 优先用于 heading 渲染，避免翻译模型把段落幻觉混入标题，
# 也避免逐篇翻译时章节名译法不一致（如 Abstract 有时译“摘要”有时译“概要”）。
_SECTION_TITLE_MAP: dict[str, str] = {
    "abstract": "摘要",
    "introduction": "引言",
    "related work": "相关工作",
    "background": "背景",
    "preliminaries": "预备知识",
    "method": "方法",
    "methodology": "方法",
    "approach": "方法",
    "model": "模型",
    "experiments": "实验",
    "experimental setup": "实验设置",
    "evaluation": "评估",
    "results": "结果",
    "discussion": "讨论",
    "analysis": "分析",
    "conclusion": "结论",
    "conclusions": "结论",
    "limitations": "局限性",
    "future work": "未来工作",
    "references": "参考文献",
    "appendix": "附录",
    "acknowledgements": "致谢",
    "acknowledgments": "致谢",
}


def _block_to_md(block: Block, translation: str, img_mapping: dict[str, str]) -> str:
    if block.kind == "title":
        return f"# {translation}\n"
    if block.kind == "heading":
        # 先用通用学术章节标题映射（领域中立、稳定，避免翻译幻觉把段落混入标题）。
        trans = _SECTION_TITLE_MAP.get(block.text.strip().lower(), translation)
        # 标题翻译易出现“把后续段落混入标题”的幻觉。合理性校验：标题应短、
        # 无段落标志词、无多个句末标点、且原文极短时译文不应膨胀为段落。
        # 任一不通过则回退到原文 block.text，保证 markdown 结构正确且不重复。
        if not _looks_like_heading(trans, block.text):
            trans = block.text
        return f"{'#' * block.level} {trans}\n"
    if block.kind in ("paragraph", "list_item"):
        return f"{translation}\n"
    if block.kind in ("equation", "figure", "table"):
        raw = block.raw
        for orig, local in img_mapping.items():
            raw = raw.replace(orig, local)
        return f"{raw}\n"
    return f"{translation}\n"


# 标题翻译合理性判定：返回 True 表示当前 translation 适合作为标题。
def _looks_like_heading(trans: str, orig: str) -> bool:
    if not trans or not trans.strip():
        return False
    t = trans.strip()
    # 原文极短（如 "Abstract"），译文长度不应膨胀为段落
    if len(orig) <= 12 and len(t) > 80:
        return False
    # 标题一般不含多个句末标点；多个句末标点几乎一定是段落。
    # 排除章节编号中的句号（如 "3.1"、"4.2.3"），这些不是句末标点。
    # 例外：原文本身就是疑问句（以 ? 结尾）的标题，允许一个问号。
    clean = re.sub(r"\d\.\d[\d.]*", "", t)  # 去除 "3.1"、"4.2.3" 等编号
    end_punct = sum(1 for c in clean if c in "。.!?！？；;…")
    orig_is_question = orig.rstrip().endswith("?")
    max_punct = 2 if not orig_is_question else 1
    if end_punct > max_punct:
        return False
    # 段落标志词：标题里不应出现的强段落线索
    paragraph_markers = (
        "我们认为", "本文认为", "本文提出", "我们提出", "我们开发",
        "在本节中", "本节中", "具体来说", "此外", "首先", "其次",
        "然而", "我们假设", "实验表明", "结果表明", "呈现", "诊断特征",
    )
    for m in paragraph_markers:
        if m in t and len(t) > len(m) + 10:
            return False
    return True


def _merge_short_blocks(blocks: list[Block], min_chars: int) -> tuple[list[Block], list[list[int]]]:
    """将过短的相邻同类型文本块组成「翻译单元」一起翻译，但各块内容保持独立。

    返回 (blocks, units)：
      - blocks：原块列表（不拼接文本，保持每个块独立用于最终渲染）。
      - units：翻译单元列表，每个单元是若干块下标（相邻同类型且后块过短则归入前块所在单元）。
        单块单元为 [i]，合并组单元为 [i, j, k, ...]。

    设计要点：
      - 仅将 paragraph↔paragraph 与 list_item↔list_item 的过短后块并入前一文本块所在单元，
        但各块文本互不拼接，翻译时以 JSON 数组分别发给模型、分别回收译文。
      - 结构块（title/heading/equation/figure/table）始终独立成单元，并作为合并链的分隔符。
      - min_chars <= 0 时关闭合并（每个块自成单元）。
    """
    if min_chars <= 0:
        return blocks, [[i] for i in range(len(blocks))]

    TEXT_KINDS = ("paragraph", "list_item")
    STRUCT_KINDS = ("title", "heading", "equation", "figure", "table")

    units: list[list[int]] = []
    last_text_unit_idx: Optional[int] = None  # 最近一个文本单元在 units 中的下标
    merged = 0

    def flush_single(i: int) -> None:
        nonlocal last_text_unit_idx
        units.append([i])
        last_text_unit_idx = None  # 占位，下面结构块会覆盖

    for i, b in enumerate(blocks):
        if b.kind in STRUCT_KINDS:
            units.append([i])
            last_text_unit_idx = None
            continue
        if b.kind in TEXT_KINDS:
            if (last_text_unit_idx is not None
                    and blocks[units[last_text_unit_idx][0]].kind == b.kind
                    and len(b.text.strip()) < min_chars):
                # 过短后块并入前一文本块所在单元（保持各自独立下标）
                units[last_text_unit_idx].append(i)
                merged += 1
                continue
            # 开新文本单元
            units.append([i])
            last_text_unit_idx = len(units) - 1
            continue
        # 未知 kind 直接保留并断开
        units.append([i])
        last_text_unit_idx = None

    if merged:
        logger.info(f"短块合并：{merged} 个过短文本块并入相邻翻译单元（以 JSON 分块翻译）")
    return blocks, units


def _download_images(html_text: str, img_dir: Path) -> dict[str, str]:
    """从 HTML 文本中解析图片并下载；返回 原始src -> 本地相对路径 映射。"""
    soup = BeautifulSoup(html_text, "html.parser")
    imgs = soup.find_all("img")
    if not imgs:
        return {}
    img_dir.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, str] = {}
    base = "https://arxiv.org/html/"

    # 只统计真正需要下载的图片，过滤掉 data: 与无效 src
    todo: list[tuple[str, str, str]] = []
    for img in imgs:
        src = img.get("src")
        if isinstance(src, list):
            src = src[0] if src else None
        if not isinstance(src, str) or src.startswith("data:") or not src.rstrip("/").split("/")[-1]:
            continue
        if src.startswith("http"):
            full = src
        elif src.startswith("/"):
            full = "https://arxiv.org" + src
        else:
            full = base + src
        fname = src.rstrip("/").split("/")[-1]
        todo.append((src, full, fname))

    success = 0
    for src, full, fname in _new_tqdm(todo, "下载图片", "图"):
        if download_binary(full, img_dir / fname):
            mapping[src] = f"{img_dir.name}/{fname}"
            success += 1
    logger.info(f"图片下载完成: {success}/{len(todo)} 成功")
    return mapping


def _text_of_block(block: Block) -> str:
    """取一个块用于翻译的文本（标题/段落/列表用 text，图表表格用 caption）。"""
    if block.kind in ("equation", "figure", "table"):
        return block.meta.get("caption") or ""
    return block.text or ""


def _build_seed_glossary(translator: LLMTranslator, blocks: list[Block]) -> Glossary:
    """单线程建表阶段：用标题 + 前几段快速建立初始术语表，供并发阶段共享。

    先注入领域默认术语种子（with_defaults），再让模型补充自创词译法。
    """
    glossary = Glossary.with_defaults()
    seed_parts: list[str] = []
    for b in blocks:
        if b.kind == "title" and b.text:
            seed_parts.append(b.text)
        elif b.kind in ("paragraph", "list_item") and b.text:
            seed_parts.append(b.text)
        if len(seed_parts) >= 4:  # 标题 + 摘要及前几段足够锁定大部分易错词
            break
    base_count = len(glossary.terms)
    if not seed_parts:
        return glossary
    text = "\n\n".join(seed_parts)
    logger.info("正在构建初始术语表（锁定专有名词/易错词译法）...")
    _, terms = translator.translate(text, glossary)
    glossary.ingest_terms(terms)
    logger.info(f"建表阶段完成，初始术语 {len(glossary.terms)} 条（含种子 {base_count} 条）")
    return glossary


def _build_paper_summary(blocks: list[Block], translator: LLMTranslator) -> str:
    """生成一句论文全局立场摘要（锚定翻译基调，防幻觉/串文）。

    零成本实现：用标题 + 摘要段落直接拼成一句简短说明；
    若摘要为空则回退到标题。
    """
    title = ""
    abstract_parts: list[str] = []
    for b in blocks:
        if b.kind == "title" and b.text:
            title = b.text
        elif b.kind == "paragraph" and b.text:
            abstract_parts.append(b.text)
        if title and len(abstract_parts) >= 3:
            break
    if title and abstract_parts:
        joined = " ".join(abstract_parts)
        if len(joined) > 600:
            joined = joined[:600] + "…"
        return (
            f"本文标题：《{title}》。摘要要点：{joined}。"
            "请据此判断本文的核心任务/方法/立场（如提出新方法、理论分析或实证研究），并始终与摘要立场保持一致。"
        )
    if title:
        return f"本文标题：《{title}》。翻译时保持全文立场与标题一致。"
    return "翻译时保持全文立场一致，不得引入与上下文相悖的幻觉内容。"


def _translate_block(translator: LLMTranslator, block: Block, glossary: Glossary,
                     summary: str = ""):
    """翻译单个块（线程安全：只读 glossary，不改写）。返回 (block, translation, terms)。"""
    text = _text_of_block(block)
    if not text.strip():
        return block, "", []
    translation, terms = translator.translate(text, glossary, summary=summary)
    return block, translation, terms


def _translate_concurrent(translator: LLMTranslator, blocks: list[Block],
                          glossary: Glossary, concurrency: int,
                          summary: str = "") -> dict[int, tuple[str, list]]:
    """并发翻译所有块。返回 {block_index: (translation, terms)}。"""
    results: dict[int, tuple[str, list]] = {}
    if concurrency <= 1:
        for i, b in _new_tqdm(list(enumerate(blocks)), "翻译", "block"):
            _, trans, terms = _translate_block(translator, b, glossary, summary)
            results[i] = (trans, terms)
        return results

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        fut_map = {
            pool.submit(_translate_block, translator, b, glossary, summary): i
            for i, b in enumerate(blocks)
        }
        for fut in _new_tqdm(as_completed(fut_map), "翻译", "block", total=len(fut_map)):
            i = fut_map[fut]
            _, trans, terms = fut.result()
            results[i] = (trans, terms)
    return results


def _translate_unit(translator: LLMTranslator, blocks: list[Block], unit: list[int],
                   glossary: Glossary, summary: str = "") -> dict[int, tuple[str, list]]:
    """翻译一个翻译单元（单块或合并组），返回 {block_index: (translation, terms)}。

    - 单块：直接调 translator.translate。
    - 合并组（>1 块）：以 JSON 数组分块发给模型，分别回收各块译文（文本互不拼接）。
    """
    if len(unit) == 1:
        i = unit[0]
        text = _text_of_block(blocks[i])
        if not text.strip():
            return {i: ("", [])}
        trans, terms = translator.translate(text, glossary, summary=summary)
        return {i: (trans, terms)}

    texts = [_text_of_block(blocks[i]) for i in unit]
    trans_list, terms = translator.translate_group(texts, glossary, summary=summary)
    out: dict[int, tuple[str, list]] = {}
    for k, i in enumerate(unit):
        out[i] = (trans_list[k] if k < len(trans_list) else "", terms)
    return out


def _translate_units(translator: LLMTranslator, blocks: list[Block], units: list[list[int]],
                    glossary: Glossary, concurrency: int,
                    summary: str = "") -> dict[int, tuple[str, list]]:
    """按翻译单元并发翻译。返回 {block_index: (translation, terms)}。"""
    results: dict[int, tuple[str, list]] = {}
    if concurrency <= 1:
        for u in _new_tqdm(units, "翻译", "unit"):
            results.update(_translate_unit(translator, blocks, u, glossary, summary))
        return results

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        fut_map = {
            pool.submit(_translate_unit, translator, blocks, u, glossary, summary): u
            for u in units
        }
        for fut in _new_tqdm(as_completed(fut_map), "翻译", "unit", total=len(fut_map)):
            u = fut_map[fut]
            results.update(fut.result())
    return results


def _retranslate_unit(translator: LLMTranslator, blocks: list[Block], unit: list[int],
                      glossary: Glossary, summary: str,
                      translations: dict[int, str]) -> None:
    """对需返修的翻译单元重新翻译，直接更新 translations（按块下标）。"""
    res = _translate_unit(translator, blocks, unit, glossary, summary)
    for i, (trans, _) in res.items():
        translations[i] = trans


def _translate_tables(translator: LLMTranslator, blocks: list[Block],
                     glossary: Glossary, concurrency: int,
                     summary: str = "") -> dict[int, str]:
    """并发翻译所有 table 块的 table_md 内容。返回 {block_index: translated_table_md}。"""
    tasks: list[tuple[int, str]] = []
    for i, block in enumerate(blocks):
        if block.kind != "table":
            continue
        table_md = block.meta.get("table_md") or ""
        if table_md:
            tasks.append((i, table_md))
        elif block.text.strip():
            tasks.append((i, block.text))
    if not tasks:
        return {}

    logger.info(f"检测到 {len(tasks)} 个表格需要单独翻译")
    results: dict[int, str] = {}

    def _do(idx_text: tuple[int, str]) -> tuple[int, str]:
        i, text = idx_text
        try:
            trans, _ = translator.translate(text, glossary, summary=summary)
        except Exception as e:
            logger.warning(f"表格 {i} 翻译失败: {e}")
            trans = text
        return i, trans

    if concurrency <= 1:
        for t in _new_tqdm(tasks, "翻译表格", "表格"):
            i, trans = _do(t)
            results[i] = trans
        return results

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = [pool.submit(_do, t) for t in tasks]
        for fut in _new_tqdm(as_completed(futs), "翻译表格", "表格", total=len(futs)):
            i, trans = fut.result()
            results[i] = trans
    return results


def _finalize_glossary(base: Glossary, all_terms: list[list[dict]]) -> Glossary:
    """合并所有块返回的术语，形成最终术语表（带冲突消解）。"""
    final = Glossary()
    final.terms = dict(base.terms)  # 继承建表阶段的锁定词（最高优先）
    for terms in all_terms:
        for item in terms or []:
            en = (item.get("en") or "").strip()
            zh = (item.get("zh") or "").strip()
            if not en or not zh:
                continue
            k = final._norm(en)
            full = (item.get("en_full") or "").strip() or None
            note = (item.get("note") or "").strip() or None
            if k not in final.terms:
                final.terms[k] = Term(zh=zh, en_full=full, note=note, seen=True)
            else:
                t = final.terms[k]
                t.seen = True
                # 冲突消解：KEEP 优先；否则保留已锁定译法
                if t.zh == KEEP_AS_IS:
                    continue
                if zh == KEEP_AS_IS:
                    t.zh = KEEP_AS_IS
                elif not t.zh:
                    t.zh = zh
                if full and not t.en_full:
                    t.en_full = full
                if note and not t.note:
                    t.note = note
    return final


# 学术违和词黑名单：机器翻译常见直译错误，命中即认为该块需返修。
# 这些是跨领域通用的中文学术规范误译/生硬表达，不绑定特定研究方向。
_AWKWARD_BLACKLIST = [
    "锐利边界",      # sharp boundaries 误译（应为清晰/陡峭边界）
    "以...机制运行", # 机翻腔
    "有原理依据",    # principled 生硬直译（应为基于理论依据的）
]

# 占位符残留：公式保护失败或分块拼接错位时会出现这些标记，必须返修。
_PLACEHOLDER_RESIDUE = [
    r"\(块\)", r"\(公式\)", r"<MATH_", r"<math", r"<MATH ", r"MATH_\d+>",
    r"\{\{MATH", r"@@MATH", r"<block>", r"\{\{BLOCK",
]


def _needs_repair(block: Block, translation: str, glossary: Glossary) -> bool:
    """一致性检查：判断某块译文是否需要返修。"""
    if not translation.strip():
        return False
    # 规则0：占位符残留（公式保护失败 / 分块错位）
    for pat in _PLACEHOLDER_RESIDUE:
        if re.search(pat, translation):
            return True
    # 规则1：学术违和词黑名单命中 -> 返修
    for bad in _AWKWARD_BLACKLIST:
        if bad in translation:
            return True
    # 规则2：标记为保留英文的词若仍出现在译文里，说明被误译 -> 返修
    for en, t in glossary.terms.items():
        if t.zh != KEEP_AS_IS:
            continue
        if re.search(rf"\b{re.escape(en)}\b", translation, re.IGNORECASE):
            return True
    # 规则3：某术语在译文中出现了英文原词，但 glossary 给了中文译法
    #       （说明模型既没用译法也没保留原文，可能不一致）
    for en, t in glossary.terms.items():
        if t.zh == KEEP_AS_IS or not t.zh:
            continue
        if re.search(rf"\b{re.escape(en)}\b", translation, re.IGNORECASE):
            # 同时检查译文中是否不含该中文译法 -> 视为未沿用
            if t.zh not in translation:
                return True
    # 规则4：纯中文术语误译（不依赖英文单词边界）。
    # 模型可能没沿用 glossary 的中文译法，而是用了近义词（如“对齐”->“齐整”）。
    # WRONG_VARIANT_MAP 收录这类已知错误中文表达，命中即视为未沿用正确译法。
    for wrong in WRONG_VARIANT_MAP:
        if wrong in translation:
            return True
    return False


def run(url_or_id: str) -> Path:
    """执行完整翻译流程，返回输出 markdown 路径。"""
    settings = get_settings()
    arxiv_id = parse_arxiv_id(url_or_id)
    logger.info(f"解析到 arxiv ID: {arxiv_id}")

    workdir = settings.output_dir / arxiv_id
    workdir.mkdir(parents=True, exist_ok=True)
    img_dir = workdir / "images"

    # 1. 下载 HTML
    url = f"https://arxiv.org/html/{arxiv_id}"
    logger.info(f"下载 HTML: {url}")
    html_text = download_text(url)
    html_path = workdir / f"{arxiv_id}.html"
    html_path.write_text(html_text, encoding="utf-8")
    logger.info(f"  已保存: {html_path}")

    # 2. 下载图片（仅在本地模式开启时下载，否则图片保持原网络 URL）
    img_mapping: dict[str, str] = {}
    if settings.image_local:
        logger.info("下载论文图片（本地模式） ...")
        img_mapping = _download_images(html_text, img_dir)
    else:
        logger.info("图片本地模式未开启，引用保持原网络 URL，跳过下载")

    # 3. 解析
    logger.info("解析 HTML 结构 ...")
    blocks = parse_arxiv_html(html_path, img_mapping=img_mapping)
    logger.info(f"  提取到 {len(blocks)} 个内容块")

    # 3.1 短块合并：过短的相邻文本块组成翻译单元一起请求（JSON 分块翻译，但各块内容保持独立）
    blocks, units = _merge_short_blocks(blocks, settings.merge_min_chars)
    logger.info(f"翻译单元: {len(units)} 个（其中合并组 "
                f"{sum(1 for u in units if len(u) > 1)} 个）")

    # 4. 翻译
    translator = LLMTranslator()
    logger.info(f"初始化翻译器 (model={translator.llm.model}, "
                f"并发={settings.translate_concurrency})")

    # 4.1 建表阶段（单线程）：锁定易错术语，供并发共享
    glossary = _build_seed_glossary(translator, blocks)

    # 4.1.1 生成论文全局立场摘要（锚定翻译基调，防幻觉/串文）
    # 摘要仅作为内部 prompt 上下文传给 translator，不向用户控制台输出全文。
    summary = _build_paper_summary(blocks, translator)

    # 4.2 并发翻译阶段（按翻译单元调度；合并组以 JSON 数组分块翻译）
    logger.info("开始并发翻译 ...")
    results = _translate_units(
        translator, blocks, units, glossary, settings.translate_concurrency, summary=summary
    )
    translations = {i: results[i][0] for i in results}
    all_terms = [results[i][1] for i in results]

    # 4.3 合并术语 + 一致性检查返修
    if settings.translate_repair:
        final_glossary = _finalize_glossary(glossary, all_terms)
        logger.info(f"合并术语表: {len(final_glossary.terms)} 条，开始一致性检查/返修 ...")
        # 按翻译单元做返修判断：合并组内任一子块需返修则整组重翻
        repair_units: list[list[int]] = []
        for u in units:
            need = False
            for i in u:
                if _needs_repair(blocks[i], translations.get(i, ""), final_glossary):
                    need = True
                    break
            if need:
                repair_units.append(u)
        if repair_units:
            logger.info(f"检出 {len(repair_units)} 个翻译单元（含 "
                        f"{sum(len(u) for u in repair_units)} 块）需返修")
            # 并发返修：各单元写不同块下标，无竞争；合并组仍以 JSON 分块翻译
            n = max(1, settings.translate_concurrency)
            if n <= 1:
                for u in _new_tqdm(repair_units, "返修", "单元"):
                    _retranslate_unit(translator, blocks, u, final_glossary, summary, translations)
            else:
                with ThreadPoolExecutor(max_workers=n) as pool:
                    futs = [
                        pool.submit(_retranslate_unit, translator, blocks, u,
                                    final_glossary, summary, translations)
                        for u in repair_units
                    ]
                    for _ in _new_tqdm(as_completed(futs), "返修", "单元", total=len(futs)):
                        pass
            logger.info(f"返修完成: {len(repair_units)} 单元")
        glossary = final_glossary
    else:
        glossary.ingest_terms(t for terms in all_terms for t in terms)

    # 4.4 组装输出
    logger.info("组装输出 ...")
    md_parts: list[str] = []
    title_text = ""
    n_translated = 0

    # 4.4.1 表格并发翻译：先收集所有 table 块的 markdown，一次性并发翻译，避免串行卡顿
    table_translations = _translate_tables(
        translator, blocks, glossary, settings.translate_concurrency, summary=summary
    )

    for i, block in enumerate(blocks):
        trans = translations.get(i, "")
        n_translated += 1
        if block.kind == "title":
            title_text = trans or block.text
            # 标题已在文件头部统一输出，避免正文中重复出现
            continue
        if block.kind in ("figure", "table"):
            cap_zh = trans
            if block.kind == "figure":
                # 图片引用路径由 parser 阶段计算好（本地模式为 images/...，
                # 非本地模式为完整网络 URL），直接使用，避免在空 img_mapping 下
                # 把相对路径当成引用导致图片加载失败。
                img_ref = block.meta.get("local_src") or block.meta.get("src") or ""
                prefix = f"![figure]({img_ref})\n\n> " if img_ref else "> "
                block.raw = (prefix + cap_zh) if cap_zh else (prefix.strip() or "(图)")
            elif block.kind == "table":
                # 表格内容（含表头）已提前并发翻译完成，直接回填；保留 Markdown 表格语法、
                # | 分隔符与列内 LaTeX 公式。
                table_md = table_translations.get(i, "")
                parts = []
                if table_md:
                    parts.append(table_md)
                if cap_zh:
                    # 学术规范：表说明（表注）置于表格下方
                    parts.append("> " + cap_zh)
                block.raw = "\n\n".join(parts) if parts else "(表格)"
            md_parts.append(_block_to_md(block, cap_zh, img_mapping))
        else:
            # equation / heading / paragraph / list_item：默认走 _block_to_md。
            # equation 无需翻译也不参与特殊拼接，直接输出 block.raw 中的完整公式。
            md_parts.append(_block_to_md(block, trans, img_mapping))

    # 5. 写出
    out_md = workdir / f"{arxiv_id}.zh.md"
    glossary_path = workdir / f"{arxiv_id}.glossary.json"
    logger.info("保存术语表与 Markdown 文件 ...")
    glossary.save(glossary_path)
    logger.info(f"术语表已保存: {glossary_path}（共 {len(glossary.terms)} 条）")

    header = (
        f"# {title_text}\n\n"
        f"> 原文: https://arxiv.org/abs/{arxiv_id}\n"
        f"> 本译文由 DeepSeek 自动翻译，公式与结构保留原文，仅供参考。\n\n---\n\n"
    )
    body = "\n".join(md_parts) + "\n"
    # 中英文/数字排版间距自动修复（pangu 风格）：先保护公式与链接，修复后再还原
    logger.info("进行中英文排版间距修复 ...")
    body = _fix_cjk_spacing(body)
    logger.info(f"写出 Markdown 文件 ({len(body):,} 字符) ...")
    out_md.write_text(header + body, encoding="utf-8")
    logger.info(f"翻译完成: 共 {n_translated} 块 -> {out_md}")
    return out_md


# ---------- 中英文排版间距修复（pangu 风格） ----------
# 在中文字符与英文字母/数字之间加一个空格，提升阅读体验。
# 通过临时保护 Markdown 链接与公式，避免破坏 [text](url) 与 $$...$$ 内部。
_LINK_RE = re.compile(r"(\[[^\]]*\]\([^)]*\))|(`[^`]*`)")
_MATH_RE = re.compile(r"(\$\$.*?\$\$)|(\$[^$\n]+?\$)")
_CJK = r"[一-鿿]"
_LAT = r"[A-Za-z0-9]"
_SP_RE = re.compile(f"({_CJK})({_LAT})|({_LAT})({_CJK})")


def _fix_cjk_spacing(text: str) -> str:
    """在中文字符与拉丁字母/数字之间插入空格（pangu 风格），不破坏公式与链接。"""
    # 1. 保护链接、行内代码与公式（一次性保护，减少长文本扫描次数）
    stashed: list[str] = []

    def _stash(m):
        stashed.append(m.group(0))
        return f"\x00S{len(stashed) - 1}\x00"

    text = _LINK_RE.sub(_stash, text)
    text = _MATH_RE.sub(_stash, text)

    # 2. 插入空格（避免已有空格重复加）
    def _add_space(m):
        a, b = m.group(0)[0], m.group(0)[1]
        return f"{a} {b}"

    text = _SP_RE.sub(_add_space, text)

    # 3. 单次正则还原所有占位符（比循环 replace 更快）
    def _restore(m):
        idx = int(m.group(1))
        return stashed[idx]

    return re.sub(r"\x00S(\d+)\x00", _restore, text)
