"""arxiv 论文翻译流水线：解析链接 -> 下载 -> 解析 -> 翻译 -> 写出 markdown。"""

import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup, Tag

from ...config import get_settings
from ...core.downloader import download_binary, download_text
from ...core.exporter import html_to_docx, html_to_pdf
from ...core.glossary import Glossary, KEEP_AS_IS, Term, WRONG_VARIANT_MAP
from ...core.translator import LLMTranslator, TABLE_UNTRANSLATABLE_MARKER
from ...logging_setup import get_logger
from .parser import Block, parse_arxiv_html

logger = get_logger()

ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5}(v\d+)?)", re.IGNORECASE)

# 翻译缓存文件格式版本。结构变化（如字段增删）时 +1，旧版本缓存会被视为不兼容而忽略。
_TRANSLATE_CACHE_VERSION = 1


class _TranslateCache:
    """翻译阶段译文断点缓存。

    把「翻译单元 -> 译文」按 block_index 增量持久化到磁盘 JSON，使异常退出后
    下次启动可恢复，避免重跑已翻译块、浪费 Token。

    设计要点：
    * 文件命名 `.<stem>.translate_cache.json`，与最终 `.zh.md` 区分（隐藏点文件）。
    * 每次完成一个 unit 即增量 flush，而非全量结束才写，保证崩溃点之后的块仍可恢复。
    * 同时记录 arxiv_id 与版本号，避免不同论文/不同格式缓存互相串用。
    * 成功后由调用方显式 clear()，否则残留文件即代表「上次异常退出产物」。
    """

    def __init__(self, path: Path, arxiv_id: str):
        self.path = path
        self.arxiv_id = arxiv_id
        self.data: dict[str, Any] = {}
        self._loaded = False

    # —— 存在性 / 加载 ——
    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> bool:
        """加载已有缓存。版本/论文不匹配时返回 False（视为不可用）。"""
        if not self.exists():
            return False
        try:
            import json
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError) as e:
            logger.warning(f"翻译缓存读取失败，将忽略并重新翻译：{e}")
            return False
        if raw.get("version") != _TRANSLATE_CACHE_VERSION:
            return False
        if raw.get("arxiv_id") != self.arxiv_id:
            return False
        self.data = raw.get("translations", {})
        self._loaded = True
        return True

    # —— 查询 / 写入 ——
    def get(self, idx: int) -> Optional[str]:
        item = self.data.get(str(idx))
        if isinstance(item, dict):
            return item.get("translation")
        return None

    def put(self, idx: int, translation: str) -> None:
        self.data[str(idx)] = {"translation": translation}

    def save(self) -> None:
        """把当前缓存原子写入磁盘（先写临时文件再 rename，避免半截文件）。"""
        import json
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            payload = {
                "version": _TRANSLATE_CACHE_VERSION,
                "arxiv_id": self.arxiv_id,
                "translations": self.data,
            }
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            logger.warning(f"翻译缓存写入失败（不影响本次翻译）：{e}")

    def clear(self) -> None:
        try:
            if self.path.is_file():
                self.path.unlink()
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            if tmp.is_file():
                tmp.unlink()
        except OSError as e:
            logger.warning(f"翻译缓存清理失败：{e}")

    def __len__(self) -> int:
        return len(self.data)


def _ask_resume(cache: _TranslateCache) -> str:
    """向用户询问断点恢复方式。返回 'resume' / 'new' / 'quit'。

    * resume_mode=ask 且标准输入是 tty：打印选项并用 input() 读取单字符。
    * 非 tty（CI / 重定向）或 resume_mode=auto：返回 'resume'。
    * resume_mode=never：返回 'new'。
    """
    mode = (get_settings().resume_mode or "ask").strip().lower()
    if mode == "never":
        return "new"
    if mode == "auto":
        return "resume"

    # ask 模式但无交互终端：退化为 auto，避免卡死
    if not sys.stdin or not hasattr(sys.stdin, "isatty") or not sys.stdin.isatty():
        logger.warning("检测到断点缓存但当前非交互终端，按 resume_mode=auto 自动恢复。")
        return "resume"

    prompt = (
        f"\n检测到上次异常退出的翻译缓存（已翻译 {len(cache)} 个块）。请选择：\n"
        f"  [r] 恢复（复用已翻译内容，仅翻译剩余部分，节省 Token）\n"
        f"  [n] 重新翻译（丢弃缓存，从头开始）\n"
        f"  [q] 退出（不执行任何操作）\n"
        f"请输入 r / n / q 后回车："
    )
    while True:
        try:
            ans = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            logger.warning("读取用户输入中断，按 resume_mode=auto 自动恢复。")
            return "resume"
        if ans in ("r", "resume"):
            return "resume"
        if ans in ("n", "new"):
            return "new"
        if ans in ("q", "quit", "exit"):
            return "quit"
        print("无法识别，请重新输入 r / n / q。")


def _new_tqdm(
    iterable_or_n: Any,
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


def _resolve_html_url(arxiv_id: str) -> tuple[str, str]:
    """解析 arxiv HTML 全文页面地址。

    arxiv 的 HTML 版本化资源位于 ``https://arxiv.org/html/<id>vN``，
    不带版本号的根路径 ``/html/<id>`` 在部分论文上会 404。为稳定获取
    “最新版本” 的 HTML，这里统一访问 abs 摘要页
    ``https://arxiv.org/abs/<id>``，解析其中指向 ``/html/`` 的链接，
    取版本号最大的那个作为 HTML 全文地址。

    返回 (html_url, base_url)：
      - html_url：可下载的 HTML 全文完整 URL（含版本号）。
      - base_url：该 HTML 文档根（用于补全相对图片路径），如
        ``https://arxiv.org/html/2603.16192v1/``。
    若 abs 页解析失败，回退为直接构造 ``/html/<arxiv_id>``。
    """
    base_id = re.sub(r"v\d+$", "", arxiv_id, flags=re.IGNORECASE)
    abs_url = f"https://arxiv.org/abs/{base_id}"
    try:
        resp = requests.get(abs_url, timeout=get_settings().download_timeout,
                            headers=get_settings().download_headers)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        # abs 页 ACCESS PAPER 区域通常有 "HTML (experimental)" 链接指向 /html/<id>vN
        best: tuple[int, str] | None = None
        for a in soup.find_all("a", href=re.compile(r"/html/" + re.escape(base_id) + r"v\d+")):
            href = a.get("href", "")
            vm = re.search(r"v(\d+)$", href)
            if not vm:
                continue
            ver = int(vm.group(1))
            cand = "https://arxiv.org" + href if href.startswith("/") else href
            if best is None or ver > best[0]:
                best = (ver, cand)
        if best:
            html_url = best[1]
            # ar5iv 的图片 src 是相对于 arxiv html 站点的相对路径，
            # 形如 ``2603.16192v1/illustration6.png``，完整 URL 为
            # ``https://arxiv.org/html/<id>vN/<file>``，故文档根为
            # ``https://arxiv.org/html/``（不带版本号子目录）。
            base_url = "https://arxiv.org/html/"
            logger.info(f"从 abs 页解析到最新 HTML 版本: {html_url}")
            return html_url, base_url
    except Exception as e:  # noqa: BLE001
        logger.warning(f"解析 abs 页获取 HTML 链接失败，回退直接构造: {e}")

    # 回退：直接使用用户输入（可能含版本号）构造
    html_url = f"https://arxiv.org/html/{arxiv_id}"
    base_url = "https://arxiv.org/html/"
    return html_url, base_url


# 文件名非法字符 -> 等价中文/全角符号映射（Windows/Linux 均不支持的字符）
_FILENAME_ILLEGAL_MAP = {
    "\\": "＼",   # 全角反斜杠
    "/": "、",     # 顿号
    ":": "：",     # 全角冒号
    "*": "＊",     # 全角星号
    "?": "？",     # 全角问号
    '"': "”",      # 右双引号（成对处理时如含左引号也替换）
    "<": "＜",     # 全角小于
    ">": "＞",     # 全角大于
    "|": "｜",     # 全角竖线
    "\n": "", "\r": "", "\t": "",
}


def _safe_filename(stem: str, max_len: int = 120) -> str:
    """把任意标题文本转为安全的文件名 stem。

    - 将文件系统非法字符替换为等价中文/全角符号（见 _FILENAME_ILLEGAL_MAP）。
    - 丢弃控制字符。
    - 去除首尾空白与句末点（避免 Windows 文件名以点结尾的问题）。
    - 截断到 max_len（避免超长路径），并去掉截断后产生的尾随空白/点。
    """
    out = []
    for ch in stem:
        if ch in _FILENAME_ILLEGAL_MAP:
            out.append(_FILENAME_ILLEGAL_MAP[ch])
        elif unicodedata.category(ch).startswith("C"):  # 控制字符
            continue
        else:
            out.append(ch)
    name = "".join(out).strip().strip(".")
    # 折叠连续空白与折叠重复全角符号（仍保留单空格）
    name = re.sub(r"\s{2,}", " ", name)
    if len(name) > max_len:
        name = name[:max_len].rstrip().rstrip(".")
    return name or "paper"


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


def _strip_trailing_punct(text: str) -> str:
    """去除字符串末尾的孤立句号（用于标题渲染前的清理）。

    LLM 翻译时经常在 JSON 输出里给标题末尾多加一个句号（"4.4 分析."、
    "计算开销."），导致目录与正文中标题出现多余句号。这里只剥离末尾的句号
    （中英文句号 . 。），章节编号里的点号（如 "4.4.1"）位于字符串内部/前面，
    不会被剥离。问号、感叹号等标题中合理存在的标点一律保留。
    """
    return text.rstrip(".。")


def _block_to_md(block: Block, translation: str, img_mapping: dict[str, str]) -> str:
    if block.kind == "title":
        # 标题强制换行，不需要 ⟦NEWPAR⟧ 标记，统一剥离以防模型误带（兼容半角 [NEWPAR]）。
        return f"# {_strip_trailing_punct(_NEWPAR_RE.sub('', translation).strip())}\n"
    if block.kind == "heading":
        # 先用通用学术章节标题映射（领域中立、稳定，避免翻译幻觉把段落混入标题）。
        trans = _SECTION_TITLE_MAP.get(block.text.strip().lower(), translation)
        # 标题翻译易出现"把后续段落混入标题"的幻觉。合理性校验：标题应短、
        # 无段落标志词、无多个句末标点、且原文极短时译文不应膨胀为段落。
        # 任一不通过则回退到原文 block.text，保证 markdown 结构正确且不重复。
        if not _looks_like_heading(trans, block.text):
            trans = block.text
        # 标题强制换行，⟦NEWPAR⟧ 标记无用，剥离；末尾孤立句末标点也剥离，
        # 确保目录与正文标题干净无句号。
        trans = _NEWPAR_RE.sub("", trans).strip()
        return f"{'#' * block.level} {_strip_trailing_punct(trans)}\n"
    if block.kind == "text_box":
        # ar5iv 把带边框的"图片化文本框"（如 Prompt 示例框）渲染成 SVG，
        # 但 svg 内嵌的 <span class="ltx_p"> 仍保留真实文本。parser 阶段
        # 把所有段落合并成 kind=text_box 的 Block，原文 raw 已是 "> ..." 引用块
        # 模板。这里把译文按行拆分，逐行加 "> " 前缀（仅 1 层），最终 markdown
        # 渲染是一个引用块；多余空行也保留 "> " 占位，避免破坏块结构。
        # 同时清除可能混入的 ⟦NEWPAR⟧ 标记（text_box 已按原文固定段落拆分渲染）。
        lines = _split_text_box_lines(_NEWPAR_RE.sub("", translation).strip())
        return "\n".join(f"> {ln}" if ln else "> " for ln in lines) + "\n"
    if block.kind == "list_item":
        # 列表项严格保持单段渲染：一个块 = 一条 list_item，
        # 即便译文里残留了 ⟦NEWPAR⟧ 标记（不该出现，兜底清洗），也不应拆段。
        # 强制 strip 防止模型混入标记破坏 markdown 列表结构。
        cleaned = _NEWPAR_RE.sub("", translation).strip()
        return f"{cleaned}\n"
    if block.kind == "paragraph":
        # LLM 段尾标记 ⟦NEWPAR⟧：一个翻译单元可能含多块，模型把"段尾"
        # 加标记、"段内连续"不加。我们按标记拆分，每段独立成段；
        # 拆出 1 段时与原行为兼容（仍是单段 + 末尾换行）。
        pieces = [p for p in _split_at_newpar(translation) if p.strip()]
        if not pieces:
            return f"{translation.strip()}\n"
        if len(pieces) == 1:
            return f"{pieces[0]}\n"
        # 多段：段之间用空行分隔，末尾单换行（避免与下一块产生双空行）
        return "\n\n".join(pieces) + "\n"
    if block.kind in ("equation", "figure", "table", "listing"):
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
    o = orig.strip()
    # 译文与原文几乎一致（视为未翻译）：标题必须译成中文，否则返回 False
    # 允许略去的差异：章节编号首尾空格、大小写、引号；用归一化后做相似度比较。
    def _norm(s: str) -> str:
        s = s.lower()
        s = re.sub(r"^[\s\-\.]+", "", s)
        s = re.sub(r"[\s\-\.]+$", "", s)
        s = re.sub(r"[\s\-\.]+", " ", s)
        return s.strip()
    if _norm(t) == _norm(o):
        return False
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


def _merge_short_blocks(blocks: list[Block], target_min: int,
                         target_max: int = 1500) -> tuple[list[Block], list[list[int]]]:
    """将相邻同类型文本块按「目标长度」组成翻译单元，减少碎片、保持上下文连贯。

    返回 (blocks, units)：
      - blocks：原块列表（不拼接文本，保持每个块独立用于最终渲染）。
      - units：翻译单元列表，每个单元是若干相邻同类文本块下标，或单个结构块下标。

    单元组织策略（贪心凑长度）：
      - 文本块类型：paragraph / list_item / text_box（text_box 虽由多段合并，但仍作为
        一个独立翻译单元参与凑长度，内部以 JSON 分块翻译其它块时它自身也独立翻译）。
      - 开启一个「开放单元」，依次把后续同类型文本块纳入，直到累计纯文本字符数
        ≥ target_min（下限，之后遇到下一个块若会越过 target_max 就先结束本单元）；
        或累计已 ≥ target_max（上限，单块超上限则独立成单元，不强行拆分）。
      - 结构块（title/heading/equation/figure/table/listing）始终独立成单元，
        并作为合并链的分隔符（强制关闭当前开放单元）。
      - 各块文本互不拼接，翻译时以 JSON 数组分别发给模型、分别回收译文。

    配置：
      - target_min <= 0 时关闭合并（每个文本块自成单元）。
      - target_max <= 0 视作不限制（仍受 target_min 触发关闭），默认 1500。
    """
    if target_min <= 0:
        return blocks, [[i] for i in range(len(blocks))]

    TEXT_KINDS = ("paragraph", "list_item", "text_box")
    STRUCT_KINDS = ("title", "heading", "equation", "figure", "table", "listing")

    def _len(b: Block) -> int:
        return len((b.text or "").strip())

    units: list[list[int]] = []
    open_unit: list[int] = []        # 当前开放单元（同类文本块累计）
    open_kind: Optional[str] = None

    def _close() -> None:
        nonlocal open_unit, open_kind
        if open_unit:
            units.append(open_unit)
            open_unit = []
            open_kind = None

    def _cur_total() -> int:
        return sum(_len(blocks[j]) for j in open_unit)

    merged_blocks = 0
    for i, b in enumerate(blocks):
        if b.kind in STRUCT_KINDS:
            _close()
            units.append([i])
            continue
        if b.kind in TEXT_KINDS:
            blen = _len(b)
            # 无开放单元：直接开新单元（单块超上限则立即关闭独立成单元）
            if not open_unit:
                open_unit = [i]
                open_kind = b.kind
                if target_max > 0 and blen >= target_max:
                    _close()
                continue
            # 类型不同：先关旧单元，本块开新单元
            if b.kind != open_kind:
                _close()
                open_unit = [i]
                open_kind = b.kind
                if target_max > 0 and blen >= target_max:
                    _close()
                continue
            # 同类：预判「纳入后」长度，决定是否先关旧单元再开新单元
            after = _cur_total() + blen
            if target_max > 0 and after > target_max:
                # 纳入会越过上限 → 先关旧单元，本块新开单元（宁可本块单独
                # 略低于下限，也不让合并组超过上限；单块超上限则独立成单元）
                _close()
                open_unit = [i]
                if blen >= target_max:
                    _close()
                continue
            # 纳入当前单元
            open_unit.append(i)
            merged_blocks += 1
            # 达标（达到上限，或已达下限且再加会越过上限）即关闭
            total = _cur_total()
            if (target_max > 0 and total >= target_max) or (
                total >= target_min and (target_max <= 0 or total + blen >= target_max)
            ):
                _close()
            continue
        # 未知 kind 直接保留并打断
        _close()
        units.append([i])

    _close()  # 收尾

    if merged_blocks:
        logger.info(f"短块合并：{merged_blocks} 个相邻文本块按目标长度 "
                    f"({target_min}-{target_max}) 凑成翻译单元（以 JSON 分块翻译）")
    return blocks, units


def _download_images(html_text: str, img_dir: Path, base: str = "https://arxiv.org/html/") -> dict[str, str]:
    """从 HTML 文本中解析图片并下载；返回 原始src -> 本地相对路径 映射。

    base: HTML 文档根 URL（含版本号，如 https://arxiv.org/html/2603.16192v1/），
          用于把相对图片路径补全为完整网络 URL。

    ar5iv 对较大/矢量图（如阈值敏感性分析的 SVG）用 <object data="*.svg"> 而非
    <img src="*.png"> 嵌入。这里同时扫描两种标签，保证本地模式下 SVG 也被下载。
    """
    soup = BeautifulSoup(html_text, "html.parser")
    # 兼容 <img src> 与 <object data> 两种资源嵌入方式（图 3 这类 SVG 走 <object>）
    refs: list[tuple[str, str]] = []
    for img in soup.find_all("img"):
        src = img.get("src")
        if isinstance(src, list):
            src = src[0] if src else None
        if isinstance(src, str) and src:
            refs.append(("img", src))
    for obj in soup.find_all("object"):
        src = obj.get("data")
        if isinstance(src, list):
            src = src[0] if src else None
        if isinstance(src, str) and src:
            refs.append(("object", src))
    if not refs:
        return {}
    img_dir.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, str] = {}

    # 只统计真正需要下载的资源，过滤掉 data: 与无效 src
    todo: list[tuple[str, str, str]] = []
    for kind, src in refs:
        if src.startswith("data:") or not src.rstrip("/").split("/")[-1]:
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


_NEWPAR_MARKER = "⟦NEWPAR⟧"
# LLM 偶发会把段尾标记误写成半角 [NEWPAR]（而非规范的全角 ⟦NEWPAR⟧），
# 直接 replace 全角版本会漏清。用正则同时兼容两种括号形态，并允许标记内
# 出现空格/忽略大小写。"NEWPAR" 为专用指令词，真实论文正文不会自然出现，
# 故直接全局清除、不额外要求两侧为空白，避免残留到最终 markdown。
_NEWPAR_RE = re.compile(r"(?:⟦|\[)\s*NEWPAR\s*(?:⟧|\])", re.IGNORECASE)


def _split_at_newpar(translation: str) -> list[str]:
    """把段落译文按 LLM 输出的 ⟦NEWPAR⟧ 段尾标记拆分为多个独立段。

    设计要点：
      - LLM 在每段独立段尾追加 ⟦NEWPAR⟧（紧贴最后字符、无空格）；
      - 我们用纯字符串 split，保留空段（用 strip 兜底）；
      - 拆分后每段都会被 markdown 输出为独立段落（前后空行），从而解决
        合并翻译后多个 paragraph 紧贴成一行的可读性问题。
    """
    if not translation:
        return [""]
    parts = _NEWPAR_RE.split(translation)
    cleaned: list[str] = []
    for p in parts:
        cleaned.append(p.strip("\n"))
    # 保证非空（即便 LLM 输出空字符串也保留一个空段，以免丢内容）
    return cleaned or [""]


def _split_text_box_lines(translation: str) -> list[str]:
    """把 text_box 译文按行拆分，逐行加 "> " 前缀。

    处理：
      - 真实换行 ("\n") → 拆成多个引用块行
      - 行内多余空行（连续 \n\n\n）→ 保留 "> " 占位，避免引用块断裂
      - 行尾空格 → 保留
    """
    s = translation.replace("\r\n", "\n")
    return s.split("\n")


def _text_of_block(block: Block) -> str:
    """取一个块用于翻译的文本（标题/段落/列表用 text，图表表格及伪代码用 caption）。"""
    if block.kind in ("equation", "figure", "table", "listing"):
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
    _t0 = time.monotonic()
    _, terms = translator.translate(text, glossary)
    glossary.ingest_terms(terms)
    logger.info(f"建表阶段完成，初始术语 {len(glossary.terms)} 条（含种子 {base_count} 条），"
                f"耗时 {time.monotonic() - _t0:.1f}s")
    return glossary


def _build_paper_summary(blocks: list[Block], translator: LLMTranslator) -> str:
    """生成论文"全局立场摘要"（锚定翻译基调，防幻觉/串文）。

    实现：优先抽取 parser 显式标记的 `section == "abstract"` 段落作为摘要正文，
    回退到"标题之后的前几段"；再调用 translator.generate_summary() 让 LLM 基于
    **标题 + 真实摘要**归纳出 任务/方法/主要发现/立场 四要素的结构化立场文本。
    若 LLM 调用失败，则回退到基于标题 + 摘要原文的轻量字符串拼接（旧行为），
    保证不阻断整篇翻译流程。
    """
    title = ""
    abstract_parts: list[str] = []
    for b in blocks:
        if b.kind == "title" and b.text:
            title = b.text
        # 精确抽取摘要：parser 已为 abstract 段落打 section="abstract" 标记
        elif b.meta and b.meta.get("section") == "abstract" and b.text:
            abstract_parts.append(b.text)

    # 回退：无显式 abstract 标记时，取标题之后前 3 个段落（兼容 ar5iv 未标记的情况）
    if not abstract_parts:
        for b in blocks:
            if b.kind == "paragraph" and b.text:
                abstract_parts.append(b.text)
            if len(abstract_parts) >= 3:
                break

    abstract_text = "\n".join(abstract_parts).strip()

    # 真正生成一次结构化全局摘要（额外一次 LLM 调用）。
    # 摘要字符上限来自配置 summary_max_abstract_chars（0 = 不截断，使用完整摘要）。
    max_chars = get_settings().summary_max_abstract_chars

    # 真正生成一次结构化全局摘要（额外一次 LLM 调用）
    if title or abstract_text:
        generated = translator.generate_summary(title, abstract_text, max_abstract_chars=max_chars)
        if generated:
            return generated.strip()

    # —— 回退路径：LLM 生成失败时的轻量锚定（不阻断流程）——
    if title and abstract_text:
        joined = abstract_text
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


def _pre_translate_abbrevs(translator: LLMTranslator, blocks: list[Block],
                            glossary: Glossary, summary: str = "") -> dict[int, str]:
    """缩写定义预热：先扫描英文原文里的“缩写 = 英文全称”定义入表，再单线程优先
    翻译含缩写定义的块（图注/脚注/定义句），把其中文译法 ingest 进术语表。

    返回 {block_index: 预翻译结果}。这些块在后续并发翻译阶段会被直接复用，
    不再重翻，从而保证表格表头等位置出现的同一缩写与图注译法一致。

    这是通用机制：不依赖任何具体论文的硬编码术语，完全由论文自身文本驱动。
    """
    # 1) 英文原文缩写定义扫描入库（仅记录 缩写 -> 英文全称）
    for b in blocks:
        text = _text_of_block(b)
        if text.strip():
            glossary.ingest_abbrev_defs(text)

    # 2) 找出“含缩写定义”的块：图注/脚注 caption，或正文里出现 “ABBR = English” 的段落
    ABBR_DEF_HINT = re.compile(r"\b[A-Z][A-Za-z0-9]{1,}\s*[:=]\s*[A-Za-z][A-Za-z0-9 \-]+")
    pre_idx: list[int] = []
    for i, b in enumerate(blocks):
        if b.kind in ("figure", "table", "listing"):  # 图注/表注/伪代码注常含缩写映射
            pre_idx.append(i)
            continue
        if b.kind in ("paragraph", "list_item") and ABBR_DEF_HINT.search(b.text or ""):
            pre_idx.append(i)
    if not pre_idx:
        return {}

    logger.info(f"缩写定义预热：优先翻译 {len(pre_idx)} 个含缩写定义的块以锁定术语")
    prefilled: dict[int, str] = {}
    for i in _new_tqdm(pre_idx, "缩写预热", "block"):
        b = blocks[i]
        text = _text_of_block(b)
        if not text.strip():
            prefilled[i] = ""
            continue
        trans, terms = translator.translate(text, glossary, summary=summary)
        glossary.ingest_terms(terms)
        glossary.ingest_translation(trans)
        prefilled[i] = trans
    return prefilled


def _translate_units(translator: LLMTranslator, blocks: list[Block], units: list[list[int]],
                    glossary: Glossary, concurrency: int,
                    summary: str = "", prefilled: Optional[dict[int, str]] = None,
                    cache: Optional["_TranslateCache"] = None) -> dict[int, tuple[str, list]]:
    """按翻译单元并发翻译。返回 {block_index: (translation, terms)}。

    prefilled: 已预翻译好的块下标 -> 译文，直接复用，不再翻译（用于缩写预热）。
    cache:     断点续译缓存（恢复模式）。命中缓存的 unit 直接复用译文、不再调 LLM，
               新完成的 unit 在结果回收时增量写入缓存，使中途崩溃后下次可恢复。
    """
    prefilled = prefilled or {}
    results: dict[int, tuple[str, list]] = {}
    pending_units = [u for u in units if not all(i in prefilled for i in u)]
    if prefilled:
        for i, trans in prefilled.items():
            results[i] = (trans, [])

    # 恢复模式：缓存已覆盖的 unit 直接复用，不进并发队列。
    if cache is not None:
        resume_units = [u for u in pending_units
                        if all(cache.get(i) is not None for i in u)]
        pending_units = [u for u in pending_units
                         if not all(cache.get(i) is not None for i in u)]
        for u in resume_units:
            for i in u:
                results[i] = (cache.get(i), [])
        if resume_units:
            logger.info(f"断点续译：跳过缓存命中的 {len(resume_units)} 个单元，"
                        f"剩余 {len(pending_units)} 个待翻译。")

    if not pending_units:
        return results

    if concurrency <= 1:
        for u in _new_tqdm(pending_units, "翻译", "unit"):
            res = _translate_unit(translator, blocks, u, glossary, summary)
            results.update(res)
            if cache is not None:
                for i, (trans, _) in res.items():
                    cache.put(i, trans)
                cache.save()
        return results

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        fut_map = {
            pool.submit(_translate_unit, translator, blocks, u, glossary, summary): u
            for u in pending_units
        }
        for fut in _new_tqdm(as_completed(fut_map), "翻译", "unit", total=len(fut_map)):
            u = fut_map[fut]
            res = fut.result()
            results.update(res)
            if cache is not None:
                for i, (trans, _) in res.items():
                    cache.put(i, trans)
                cache.save()
    return results


def _retranslate_unit(translator: LLMTranslator, blocks: list[Block], unit: list[int],
                      glossary: Glossary, summary: str,
                      translations: dict[int, str]) -> None:
    """对需返修的翻译单元重新翻译，直接更新 translations（按块下标）。"""
    res = _translate_unit(translator, blocks, unit, glossary, summary)
    for i, (trans, _) in res.items():
        translations[i] = trans


def _split_gfm_cells(line: str) -> list[str]:
    """将 GFM 表格行按 | 拆分为 cell 列表，处理 \\| 转义与 $...$ 内管道。"""
    if not line.strip().startswith("|"):
        return []
    # 去掉首尾 |
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    cells: list[str] = []
    depth_math = 0
    buf: list[str] = []
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch == "\\" and i + 1 < len(inner) and inner[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if ch == "$":
            depth_math ^= 1
            buf.append(ch)
            i += 1
            continue
        if ch == "|" and depth_math == 0:
            cells.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    cells.append("".join(buf).strip())
    return cells


_CJK_CHAR_RE = re.compile(r"[\u4E00-\u9FFF\u3400-\u4DBF]")
_NUMBER_ONLY_RE = re.compile(r"^[\d.\-\s,%†‡∗*†‡]+$")


def _cell_is_translatable(cell: str) -> bool:
    """判断 GFM 表格 cell 是否含有需要翻译的英文文本。

    跳过：纯数字/符号/LaTeX/空/全大写短缩写/已含中文的 cell。
    为避免学术表格中的"单词专有名词/缩写"（如 ASR、Ordered 这种数据类别名）
    被错误地强制译为全中文，对**单词（cell 内无空格）且 ≤ 2 个 token** 的
    cell 跳过；但对**多词短语**一律翻译（含表头多词单元格如 "History Order"
    "Module Name" 等）。
    """
    if not cell or not cell.strip():
        return False
    text = cell.strip()
    # 0) 检测整 cell 是否被 <strong>/<b> 包成"加粗专名"（模型/方法/产品名）
    #    如 <strong>Qwen</strong> / <b>Llama</b>。这类在 ar5iv 表格中作列头或
    #    行标签，LLM 容易误译（Qwen→千问、Llama→美洲驼），代码侧强制保留。
    #    必须在"HTML 标签剥除"之前判断，否则标签会被后续步骤删掉导致漏判。
    _BOLD_RE = re.compile(r"^\s*<(?:strong|b)>([^<]+)</(?:strong|b)>\s*$",
                          re.IGNORECASE)
    _bm = _BOLD_RE.match(text)
    if _bm:
        _inner = _bm.group(1).strip()
        _iw = _inner.split()
        if len(_iw) == 1:
            _bs = _iw[0]
            # 首字母大写、其余全小写、长度 2-12（典型模型/品牌名：Qwen/Llama/Claude）
            if re.fullmatch(r"[A-Z][a-z]+", _bs) and 2 <= len(_bs) <= 12:
                return False
            # CamelCase（如 SageClassifier / MixtralXX）
            if re.fullmatch(r"[A-Z][a-z]+(?:[A-Z][a-z]*)+", _bs) and len(_bs) <= 12:
                return False
    # 1) 剥除常见格式标记，否则后续词拆分会被标签污染
    #    - markdown **bold** / *italic*
    #    - HTML <strong> <b> <em> <i> <u>（parser 在表格里把 ** 规范化为 <strong>）
    text_no_fmt = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text_no_fmt = re.sub(r"\*([^*]+)\*", r"\1", text_no_fmt)
    text_no_fmt = re.sub(r"<(?:strong|b|em|i|u)>([^<]*)</(?:strong|b|em|i|u)>",
                          r"\1", text_no_fmt, flags=re.IGNORECASE)

    # 2) 纯数字/符号/单位 → 跳过
    if _NUMBER_ONLY_RE.match(text_no_fmt):
        return False
    # 3) 已含中文 → 跳过（已翻译过，或中文专有名词）
    if _CJK_CHAR_RE.search(text_no_fmt):
        return False
    # 4) 去掉 LaTeX $...$，避免公式影响判断
    text_no_math = re.sub(r"\$[^$\n]+?\$", "", text_no_fmt)
    if not text_no_math.strip():
        return False

    words = text_no_math.strip().split()
    # 5) 至少含一个字母长度 ≥ 3 的"词"，否则说明全是符号/数字 → 跳过
    has_word = any(re.search(r"[A-Za-z]", w) and len(re.sub(r"[^A-Za-z]", "", w)) >= 3
                   for w in words)
    if not has_word:
        return False
    # 6) 检测"版本号/模型名 + 数字"（如 Qwen2.5-7B、Llama3-8B、GPT-4o、
    #    Claude-3.5、SAGE-v2 等）。这类整 cell 只由"字母+数字+.-_"
    #    构成、首字符为字母、含至少一个数字，一律视作专名 → 跳过。
    if len(words) == 1:
        single = text_no_math.strip()
        if (re.fullmatch(r"[A-Za-z][A-Za-z0-9.\-_]*\d[A-Za-z0-9.\-_]*", single)
                and any(c.isalpha() for c in single)
                and not " " in single):
            return False
    # 7) 全大写短缩写（GCG / PRP / ASR 等）→ 不翻译。
    #    注：对单词 cell 而言，prompt 已经要求 LLM 保留专名，所以这里不再
    #    单独保护首字母大写单词（如 Qwen / Llama / Half / Linear 等）——
    #    留给 LLM 自行判断，避免代码侧过度保守。
    alpha_tokens = [re.sub(r"[^A-Za-z]", "", w) for w in words]
    alpha_tokens = [w for w in alpha_tokens if w]
    if alpha_tokens:
        all_upper = all(w.isupper() and len(w) <= 5 for w in alpha_tokens)
        if all_upper:
            return False
    # 8) 单词级"专名"补强：cell 整体无空格（单一 token）、长度 2-25，并且
    #    整体由字母/数字 + 限定符号 (- . /) 组成（典型如 Gemini-pro、
    #    Llama2、Qwen2.5、GPT-3.5、Claude-3 等模型/产品名）。
    #    这类词由 LLM "自由发挥"翻译极易出错（Qwen→千问、Llama→美洲驼，
    #    或直接输出一段不相关的"补充介绍"），必须视作专名保留原文。
    #    例外：含空格的短语、含中文、长度超 25 都继续走 LLM 翻译。
    if len(words) == 1:
        single = text_no_math.strip()
        if 2 <= len(single) <= 25 and re.fullmatch(
            r"[A-Za-z][A-Za-z0-9._/\-]*", single
        ):
            return False
    return True


def _translate_html_table(
    table_html: str,
    translator: LLMTranslator,
    glossary: Glossary,
    summary: str = "",
) -> str:
    """对 HTML <table> 字符串做 cell 级别翻译，逐 cell 用 ASCII 占位符
    `<<<TCELL_R_C>>>` 替换原文，批量翻译后再回填。保留 colspan/rowspan/
    <thead>/<tbody>/双层表头/分组标识行等所有结构。
    """
    raw = table_html.strip()
    if not (raw.startswith("<table") or "<table" in raw[:32]):
        return table_html
    soup = BeautifulSoup(raw, "html.parser")
    table = soup.find("table")
    if table is None:
        return table_html

    rows = table.find_all("tr", recursive=False)
    translatable_cells: list[tuple[int, int, Tag]] = []
    placeholder_to_text: dict[str, str] = {}
    for ri, row in enumerate(rows):
        cells = row.find_all(["td", "th"], recursive=False)
        for ci, cell in enumerate(cells):
            inner_text = cell.get_text(" ", strip=True)
            if not _cell_is_translatable(inner_text):
                continue
            placeholder = f"<<<TCELL_{ri}_{ci}>>>"
            translatable_cells.append((ri, ci, cell))
            placeholder_to_text[placeholder] = inner_text

    if not placeholder_to_text:
        return table_html

    logger.info(f"    HTML 表格：共 {len(rows)} 行，{len(placeholder_to_text)} 个可译 cell")

    # 先把所有可译 cell 文本替换为占位符，避免 LLM 输出把 cell 间混淆。
    for ri, ci, cell in translatable_cells:
        ph = f"<<<TCELL_{ri}_{ci}>>>"
        # 提取 cell 内部：保留所有 HTML（strong/b/sup/...），只在最深文本节点上
        # 做替换。简化做法：序列化 cell HTML、把最末一个文本节点外的内容剥掉。
        # 实际更稳的方法：把 cell 内容用一个 sentinel nested tag 包裹。
        # 我们采用：直接清掉 cell 的所有文本节点，只留一个文本节点 = ph。
        for child in list(cell.contents):
            cell.extract()
        cell.append(soup.new_string(ph))

    skeleton = str(table)
    translatable_count = len(placeholder_to_text)

    # 把 skeleton 里的占位符切出供 LLM 翻译。
    placeholders = list(placeholder_to_text.keys())
    batch_payload = "\n".join(placeholder_to_text[p] for p in placeholders)
    try:
        translated_lines = translator.translate(batch_payload, glossary)
    except Exception as e:
        logger.warning(f"HTML 表格 cell 翻译失败：{e}；保留原表")
        return table_html

    translated = translated_lines.split("\n") if "\n" in translated_lines else [translated_lines]
    translated = [t for t in translated if t.strip() != ""]
    if len(translated) < translatable_count:
        # LLM 给出更少行：补齐占位为原文。
        translated.extend([""] * (translatable_count - len(translated)))

    ordered_ph = placeholders[: len(translated)]
    mapping = {
        ph: translated[idx] if translated[idx].strip() else placeholder_to_text[ph]
        for idx, ph in enumerate(ordered_ph)
    }
    for ph, line in mapping.items():
        skeleton = skeleton.replace(ph, line)

    return skeleton


def _translate_table_cells(
    table_md: str,
    translator: LLMTranslator,
    glossary: Glossary,
    summary: str = "",
) -> str:
    """对 GFM 表格做 cell 级别翻译：只翻译含英文文本的 cell，保留结构 / 数字 / LaTeX。

    策略：
      - 解析表格为行×列网格。
      - 对每个 translatable cell 调用 translator.translate，LaTeX 由 protect_math 保护。
      - 重建 GFM 表格字符串。
      - 若表格无可译 cell，直接返回原文。
    """
    lines = table_md.strip().split("\n")
    if len(lines) < 2:
        return table_md

    # 解析为网格（保留分隔行位置信息）
    grid: list[list[str]] = []
    sep_idx: int | None = None
    for ln in lines:
        cells = _split_gfm_cells(ln)
        if not cells:
            continue
        # 检测分隔行（如 | --- | --- |）
        if all(re.match(r"^:?-{3,}:?$", c) for c in cells):
            grid.append(cells)
            sep_idx = len(grid) - 1
        else:
            grid.append(cells)

    if not grid or sep_idx is None:
        return table_md

    # 收集所有需要翻译的 cell（row, col）
    todo: list[tuple[int, int, str]] = []
    for ri, row in enumerate(grid):
        if ri == sep_idx:  # 分隔行跳过
            continue
        for ci, cell in enumerate(row):
            if _cell_is_translatable(cell):
                todo.append((ri, ci, cell))

    if not todo:
        return table_md  # 没有可翻译 cell，直接返回原表

    # 翻译每个 cell（cell_mode=True：提示词强制"只翻译、不解释"，
    # 空/乱码单元格返回固定标记 TABLE_UNTRANSLATABLE_MARKER 而非胡编）。
    translated: dict[tuple[int, int], str] = {}
    for ri, ci, cell_text in todo:
        try:
            trans, _ = translator.translate(
                cell_text, glossary, summary=summary, cell_mode=True
            )
        except Exception as e:
            logger.warning(f"表格 cell({ri},{ci}) 翻译失败: {e}")
            trans = cell_text
        # 干净回退：仅当译文就是"无法翻译"固定标记时才回退原文。
        # 不依赖任何启发式（长度/正则/句号数），因此无误伤风险。
        if trans.strip() == TABLE_UNTRANSLATABLE_MARKER:
            logger.info(
                f"表格 cell({ri},{ci}) 模型判定无可译内容，回退原文: {cell_text!r}"
            )
            trans = cell_text
        translated[(ri, ci)] = trans

    # 重建表格
    out_lines: list[str] = []
    for ri, row in enumerate(grid):
        new_row: list[str] = []
        for ci, cell in enumerate(row):
            if ri == sep_idx:
                new_row.append(cell)  # 分隔行原样
            else:
                new_row.append(translated.get((ri, ci), cell))
        out_lines.append("| " + " | ".join(new_row) + " |")
    return "\n".join(out_lines)


def _translate_tables(translator: LLMTranslator, blocks: list[Block],
                     glossary: Glossary, concurrency: int,
                     summary: str = "") -> dict[int, str]:
    """并发翻译所有 table 块：以 cell 粒度翻译文本内容，保留数字 / 公式 / 结构。
    复杂表格（含 colspan/rowspan/分组行）走 HTML 路径，整体 <table> 保留并
    嵌入 markdown；简单表格仍走 GFM 路径。"""
    tasks: list[tuple[int, str, bool]] = []  # (idx, payload, is_html)
    for i, block in enumerate(blocks):
        if block.kind != "table":
            continue
        table_md = block.meta.get("table_md") or ""
        if table_md:
            is_html = table_md.lstrip().startswith("<table")
            tasks.append((i, table_md, is_html))
        elif block.text.strip():
            tasks.append((i, block.text, False))
    if not tasks:
        return {}

    html_n = sum(1 for _, _, h in tasks if h)
    gfm_n = len(tasks) - html_n
    if html_n and gfm_n:
        logger.info(f"检测到 {len(tasks)} 个表格（{html_n} 复杂 → HTML / {gfm_n} 简单 → GFM），以 cell 粒度翻译 ...")
    elif html_n:
        logger.info(f"检测到 {len(tasks)} 个复杂表格（含 colspan/rowspan），走 HTML 路径以保留结构 ...")
    else:
        logger.info(f"检测到 {len(tasks)} 个表格，以 cell 粒度翻译 ...")
    results: dict[int, str] = {}

    translatable_count = 0
    for _, payload, is_html in tasks:
        if is_html:
            soup = BeautifulSoup(payload, "html.parser")
            table = soup.find("table")
            if table is None:
                continue
            for cell in table.find_all(["td", "th"]):
                if _cell_is_translatable(cell.get_text(" ", strip=True)):
                    translatable_count += 1
        else:
            for line in payload.split("\n"):
                cells = _split_gfm_cells(line)
                for cell in cells:
                    if _cell_is_translatable(cell):
                        translatable_count += 1
    logger.info(f"  其中 {translatable_count} 个 cell 含英文文本需翻译，其余保留原文")

    def _do(idx_payload: tuple[int, str, bool]) -> tuple[int, str]:
        i, payload, is_html = idx_payload
        try:
            if is_html:
                trans = _translate_html_table(payload, translator, glossary, summary)
            else:
                trans = _translate_table_cells(payload, translator, glossary, summary)
        except Exception as e:
            logger.warning(f"表格 {i} cell-level 翻译失败，回退原文: {e}")
            trans = payload
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

    # 1. 下载 HTML（通过 abs 页解析最新版本化 HTML 地址，规避无版本号 404）
    html_url, html_base = _resolve_html_url(arxiv_id)
    logger.info(f"下载 HTML: {html_url}")
    html_text = download_text(html_url)
    html_path = workdir / f"{arxiv_id}.html"
    html_path.write_text(html_text, encoding="utf-8")
    logger.info(f"  已保存: {html_path}")

    # 2. 下载图片（仅在本地模式开启时下载，否则图片保持原网络 URL）
    img_mapping: dict[str, str] = {}
    if settings.image_local:
        logger.info("下载论文图片（本地模式） ...")
        img_mapping = _download_images(html_text, img_dir, base=html_base)
    else:
        logger.info("图片本地模式未开启，引用保持原网络 URL，跳过下载")

    # 3. 解析
    logger.info("解析 HTML 结构 ...")
    blocks, html_soup = parse_arxiv_html(html_path, img_mapping=img_mapping, base_url=html_base)
    logger.info(f"  提取到 {len(blocks)} 个内容块")

    # 记录原论文英文标题（用于 title 命名模式）
    orig_title = ""
    for b in blocks:
        if b.kind == "title" and b.text:
            orig_title = b.text
            break

    # 3.1 短块合并：过短的相邻文本块组成翻译单元一起请求（JSON 分块翻译，但各块内容保持独立）
    blocks, units = _merge_short_blocks(
        blocks, settings.merge_min_chars, settings.merge_target_max)
    logger.info(f"翻译单元: {len(units)} 个（其中合并组 "
                f"{sum(1 for u in units if len(u) > 1)} 个）")

    # 4. 翻译
    translator = LLMTranslator()
    logger.info(f"初始化翻译器 (model={translator.llm.model}, "
                f"并发={settings.translate_concurrency})")

    # 4.0 断点续译：检测上次异常退出的翻译缓存。
    # 缓存以 arxiv_id 为唯一基（与最终文件名命名模式无关），避免不同论文串用。
    cache = _TranslateCache(workdir / f".{arxiv_id}.translate_cache.json", arxiv_id)
    resume = False
    if cache.load():  # 加载成功 = 存在且版本/论文匹配
        choice = _ask_resume(cache)
        if choice == "quit":
            logger.info("用户选择退出，不执行翻译。")
            raise SystemExit(0)
        if choice == "new":
            logger.info("用户选择重新翻译，丢弃已有缓存。")
            cache.clear()
        else:  # resume
            resume = True
            logger.info(f"恢复模式：复用缓存中已翻译的 {len(cache)} 个块，"
                        f"仅翻译剩余块。")
    else:
        # 存在但不兼容（版本/论文不匹配）的残留缓存直接清掉，避免干扰
        if cache.exists():
            cache.clear()

    # 4.1 建表阶段（单线程）：锁定易错术语，供并发共享
    glossary = _build_seed_glossary(translator, blocks)

    # 4.1.1 生成论文全局立场摘要（锚定翻译基调，防幻觉/串文）
    # 摘要仅作为内部 prompt 上下文传给 translator，不向用户控制台输出全文。
    logger.info("正在生成论文全局立场摘要（基于标题 + 完整摘要，一次 LLM 调用）...")
    _t0 = time.monotonic()
    summary = _build_paper_summary(blocks, translator)
    logger.info(f"全局立场摘要生成完成，耗时 {time.monotonic() - _t0:.1f}s")

    # 4.1.2 缩写定义预热：扫描英文原文缩写定义 + 优先翻译图注/脚注等含缩写块，
    # 使术语表先锁定缩写译法，保证后续表格表头与图注一致。通用机制，无硬编码。
    prefilled = _pre_translate_abbrevs(translator, blocks, glossary, summary=summary)

    # 4.2 并发翻译阶段（按翻译单元调度；合并组以 JSON 数组分块翻译）
    # 恢复模式下 _translate_units 会跳过缓存中已有的块，并把新完成块增量写入缓存。
    logger.info("开始并发翻译 ...")
    results = _translate_units(
        translator, blocks, units, glossary, settings.translate_concurrency,
        summary=summary, prefilled=prefilled, cache=cache,
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
            # 标题仅在文件头部 (header) 输出，需单独清洗段尾换行标记 ⟦NEWPAR⟧
            # （body 的标题分支会清洗，但 header 里的 title_text 不走该分支，易遗漏）。
            title_text = _NEWPAR_RE.sub("", trans or block.text).strip()
            # 标题已在文件头部统一输出，避免正文中重复出现
            continue
        if block.kind in ("figure", "table", "listing"):
            cap_zh = trans
            if block.kind == "figure":
                # 图片引用路径由 parser 阶段计算好（本地模式为 images/...，
                # 非本地模式为完整网络 URL），直接使用，避免在空 img_mapping 下
                # 把相对路径当成引用导致图片加载失败。
                img_ref = block.meta.get("local_src") or block.meta.get("src") or ""
                prefix = f"![figure]({img_ref})\n\n> " if img_ref else "> "
                block.raw = (prefix + cap_zh) if cap_zh else (prefix.strip() or "(图)")
            elif block.kind == "listing":
                # 伪代码块：代码内容以两列 GFM 表格原样保留（行号列 + 代码列），
                # 仅替换 caption 译文放在表格下方。
                code_block = block.meta.get("listing_md") or ""
                parts = []
                if code_block:
                    parts.append(code_block)
                if cap_zh:
                    parts.append("> " + cap_zh)
                block.raw = "\n\n".join(parts) if parts else "(伪代码)"
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
    # 输出文件名命名方式：id / title / title_zh（非法字符自动换为等价中文符号）
    mode = (settings.output_name_mode or "id").strip().lower()
    if mode == "title":
        out_stem = _safe_filename(orig_title or arxiv_id)
    elif mode == "title_zh":
        out_stem = _safe_filename(title_text or orig_title or arxiv_id)
    else:  # id（默认）
        out_stem = arxiv_id
    glossary_path = workdir / f"{out_stem}.glossary.json"
    logger.info(f"输出文件命名方式: {mode}（文件名基: {out_stem}）")
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

    # 兜底：清除译文里可能残留的段尾换行标记（理论上已被 _block_to_md 拆分消费，
    # 但模型偶发漏加/误加、或误写成半角 [NEWPAR] 时仍保证最终 markdown 干净无标记字面）。
    body = _NEWPAR_RE.sub("", body)

    # 5.1 生成保留原 HTML 结构（表格合并/颜色/图片）的 .zh.html。
    # 注意：DOCX / PDF 导出功能尚未开发完毕，暂时禁用，此处仅作为 Markdown 的中间产物。
    # 关键：生成 .zh.html 时 _build_zh_html 会对每个块/表格 cell 串行走 LLM 重翻一遍
    # （保留内联颜色），耗时很长；而它唯一的消费者是尚未启用的 DOCX/PDF 导出。
    # 因此未开启 export_formats 时直接跳过，避免无谓的 5+ 分钟 LLM 调用与卡顿。
    zh_html_path = None
    if (settings.export_formats or "").strip():
        zh_html_path = workdir / f"{out_stem}.zh.html"
        try:
            _build_zh_html(
                html_soup, blocks, translations, table_translations,
                glossary, img_mapping, zh_html_path, translator, summary,
            )
        except Exception as e:
            logger.warning(f"生成 .zh.html 失败: {e}")
            zh_html_path = None
    else:
        logger.info("未开启导出格式（export_formats 为空），跳过 .zh.html 生成"
                    "（其依赖逐块 LLM 重翻，目前消费者 DOCX/PDF 导出尚未启用）")

    out_md = workdir / f"{out_stem}.zh.md"
    if settings.output_markdown:
        logger.info(f"写出 Markdown 文件 ({len(body):,} 字符) ...")
        out_md.write_text(header + body, encoding="utf-8")
        logger.info(f"翻译完成: 共 {n_translated} 块 -> {out_md}")
    else:
        logger.info(f"已跳过 Markdown 输出（output_markdown=False），仅导出: "
                    f"{(settings.export_formats or '').strip() or '(无)'}")
        out_md = None

    # 6. 导出额外格式（DOCX / PDF）—— 功能尚未开发完毕，暂时禁用
    # _export_formats(out_md, zh_html_path, settings)

    # 7. Token 用量报告（可配置开启）
    if settings.token_report:
        for line in translator.usage.report_lines(model=translator.llm.model):
            logger.info(line)

    # 8. 翻译完整成功：清理断点缓存，避免残留文件被下次误判为「异常退出产物」。
    if cache is not None:
        cache.clear()

    return out_md or zh_html_path


# ---------- 生成保留原 HTML 结构的 .zh.html（供 DOCX/PDF 导出） ----------
_CJK_CHAR = re.compile(r"[一-鿿]")


def _has_cjk(text: str) -> bool:
    return bool(_CJK_CHAR.search(text or ""))


def _translate_tag_keep_color(tag, translator, glossary, summary: str) -> None:
    """把 tag 内的英文文本逐父级翻译并写回，保留内联样式（如 color）。

    对每个"文本叶子父元素"单独翻译一次：带 color 的内联 span 翻译后仍保留
    其 style 颜色；普通段落文本则整体替换。已含中文的内容跳过（已译）。
    """
    # 按"直接父元素"分组所有文本叶子，减少翻译调用并保持结构/颜色
    groups: dict = {}
    for s in tag.find_all(string=True):
        if not str(s).strip():
            continue
        parent = getattr(s, "parent", None)
        # 防御：个别游离/异常节点（无 parent 或非 Tag）跳过，避免
        # 'tuple' object has no attribute 'parent' 之类异常中断整段回填。
        if parent is None or not isinstance(parent, Tag):
            continue
        groups.setdefault(parent, []).append(s)
    for parent, strs in groups.items():
        joined = "".join(str(x) for x in strs)
        if _has_cjk(joined):
            continue
        try:
            # translate 返回 (译文, 术语列表)，此处只需译文文本
            zh = translator.translate(joined, glossary, summary=summary)[0]
        except Exception:
            continue
        if not zh:
            continue
        for x in strs:
            try:
                x.extract()
            except Exception:
                pass
        try:
            parent.append(zh)
        except Exception:
            continue


def _build_zh_html(soup, blocks, translations, table_translations,
                   glossary, img_mapping, out_html: Path, translator, summary: str = "") -> None:
    """在原始 HTML（已带 data-zh-id）基础上回填译文，写出 .zh.html。

    保留原 HTML 的表格合并（colspan/rowspan）、内联文字颜色、图片结构，
    仅把可翻译文本替换为中文。图片路径：本地模式下已替换为本地相对路径，
    导出时由 exporter 进一步内嵌。
    """
    for i, block in enumerate(blocks):
        hid = block.meta.get("html_id")
        if not hid:
            continue
        el = soup.find(attrs={"data-zh-id": hid})
        if el is None:
            continue

        if block.kind == "table":
            # 逐单元格翻译（保留 colspan/rowspan 合并结构），颜色也保留
            for cell in el.find_all(["td", "th"]):
                _translate_tag_keep_color(cell, translator, glossary, summary)
            # 表标题（caption）
            cap = el.find(class_="ltx_caption") or el.find("caption")
            if cap and translations.get(i) and not _has_cjk(cap.get_text(" ", strip=True)):
                cap.clear()
                cap.append(translations[i])
            continue

        if block.kind in ("figure", "listing"):
            # 图片/算法：只翻译图注（caption），图片本身不翻译
            cap = el.find(class_="ltx_caption") or el.find("figcaption")
            if cap and translations.get(i) and not _has_cjk(cap.get_text(" ", strip=True)):
                cap.clear()
                cap.append(translations[i])
            # 本地图片模式下，img src 已在 parse 阶段替换为本地相对路径
            continue

        # 公式：保留原公式（LaTeX），不翻译内容；如需中文标签可忽略
        if block.kind == "equation":
            continue

        # 段落 / 标题 / 列表项：整体回填译文，尽量保留颜色
        src = translations.get(i, "")
        if src and not _has_cjk(el.get_text(" ", strip=True)):
            _translate_tag_keep_color(el, translator, glossary, summary)

    # 把无用的导航/参考文献/页脚等区域删除，减小导出体积（保留正文即可）
    for sel in ("ltx_page_navbar", "ltx_bibliography", "ltx_page_footer",
                "ltx_appendix", "ltx_errors", "ltx_page_logo"):
        for node in soup.find_all(class_=sel):
            node.extract()

    out_html.write_text(str(soup), encoding="utf-8")


def _export_formats(md_path: Optional[Path], zh_html_path: Optional[Path], settings) -> None:
    """根据配置将译文导出为 DOCX / PDF（可选）。

    DOCX/PDF 优先基于保留原 HTML 结构的 .zh.html（支持表格合并单元格、
    内联文字颜色、图片内嵌），若缺失则退化为基于 markdown 导出。
    """
    fmts = (settings.export_formats or "").strip().lower()
    if not fmts:
        return

    parts = [f.strip() for f in fmts.replace("，", ",").split(",")]
    want_docx = "docx" in parts or "docx_pdf" in parts or "all" in parts
    want_pdf = "pdf" in parts or "docx_pdf" in parts or "all" in parts

    if want_docx:
        try:
            if zh_html_path and zh_html_path.exists():
                html_to_docx(zh_html_path)
            elif md_path:
                html_to_docx(md_path)
            else:
                logger.warning("无可用源文件，跳过 DOCX 导出")
        except Exception as e:
            logger.warning(f"DOCX 导出失败，已跳过: {e}")
    if want_pdf:
        try:
            if zh_html_path and zh_html_path.exists():
                html_to_pdf(zh_html_path)
            elif md_path:
                html_to_pdf(md_path)
            else:
                logger.warning("无可用源文件，跳过 PDF 导出")
        except Exception as e:
            logger.warning(f"PDF 导出失败，已跳过: {e}")


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
