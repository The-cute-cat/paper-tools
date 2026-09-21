"""Markdown → 自包含 Markdown（图片内联为 base64）。

把 markdown 里的本地/网络图片统一转成 base64 data URI，用 ``<img>`` 标签写回，
得到一个「单文件、可直接发给别人」的 markdown：对方不需要同时拿到 images/
目录，也不依赖网络。

设计要点：

* **不重新序列化**。现有 :func:`parse_markdown` → docx/pdf 是单向且有损的管线
  （会丢 HTML 标签、归一化文本、降级公式）。若复用它再生成 markdown，正文会被
  改写。因此这里直接在**源文本**上做定点替换：除图片引用外逐字节保持原样。
* **不碰代码**。围栏代码块（``` / ~~~）与行内代码 ``` ` ``` 一律跳过，避免把
  文档里作为示例的 ``![](url)`` 也内联掉。
* **可重复执行**。已经是 ``data:`` URI 的图片原样保留，重复运行不会二次编码。
* **复用现有图片管线**（:func:`_resolve_image`）：本地相对路径、网络下载、代理、
  重试、4xx 负缓存都由它处理，因此同一份 markdown 在 docx / pdf / portable
  三种输出之间共享 ``.md_export/images`` 缓存。
* **SVG 默认栅格化为 PNG**：``data:image/svg+xml`` 属于「活动内容」（可含脚本），
  带 HTML 消毒的渲染环境可能把它过滤掉。若确定接收方用 Typora / VS Code /
  Obsidian 这类浏览器内核预览，可置 ``PAPER_TOOLS_MD_PORTABLE_KEEP_SVG=1``
  保留矢量（放大不糊）。
* **超长 data URI 只告警、不压缩**：不同渲染器对单个 data URI 长度的容忍度差别
  很大（实测 Typora 能渲染 64 KB，256 KB 起会解析失败并把整段 ``<img>`` 当纯文本
  显示——图片本身有效）。这类差异属于渲染器限制，因此本模块保持原图画质、仅在
  超过 ``_SINGLE_URI_WARN_BYTES`` 时提示，由使用者决定是否压缩或换渲染器。
* **不处理公式**：``$...$`` / ``$$...$$`` 原样保留，依赖接收方渲染器支持 LaTeX。
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from pathlib import Path

from ...logging_setup import get_logger
from .converter import (
    _FENCE_RE,
    _REF_DEF_RE,
    ConvertCtx,
    _docx_safe_image,
    _resolve_image,
)

logger = get_logger()

# 浏览器/markdown 渲染器普遍支持的图片格式；其余（tiff 等）转为 PNG。
_HTML_OK_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}

# 超过该体积给出提示：base64 会让文件膨胀约 1/3，较大的文件在聊天工具/邮件里
# 可能被拒绝或预览卡顿。
_SIZE_WARN_BYTES = 5 * 1024 * 1024

# 单张图片 data URI 长度的告警阈值（保守线，不代表硬性上限）。
# 实测 2026-09：Typora 能渲染 64 KB 的 data URI，但 256 KB 会解析失败——把整段
# <img src="data:..."> 当纯文本显示（注意：图片本身有效，不是解码错误）。
# 不同渲染器差异很大，VS Code / Obsidian / 浏览器通常不受此限，因此这里只提示、
# 不压缩图片，避免替用户牺牲画质。
_SINGLE_URI_WARN_BYTES = 64 * 1024

# 行内式图片：![alt](src "title")，src 允许用 <...> 包裹（含空格时）
_MD_IMG_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\("
    r"\s*(?:<(?P<src_angle>[^>]*)>|(?P<src>[^)\s]+))"
    r"(?:\s+(?P<title>\"[^\"]*\"|'[^']*'|\([^)]*\)))?"
    r"\s*\)"
)
# HTML 图片标签（含自闭合）
_HTML_IMG_RE = re.compile(r"<img\b[^>]*?>", re.I)
_SRC_ATTR_RE = re.compile(r"""src\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
_ALT_ATTR_RE = re.compile(r"""alt\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_TITLE_ATTR_RE = re.compile(r"""title\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
# 引用式图片 ![alt][ref]
_REF_IMG_USE_RE = re.compile(r"!\[([^\]]*)\]\[([^\]]*)\]")
# 行内代码 `...`（含多反引号；不跨行）
_CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")

_PLACEHOLDER_RE = re.compile(r"⟦MDCODE(\d+)⟧")


@dataclass
class PortableStats:
    """内联结果统计，用于日志汇总。"""
    inlined: int = 0                        # 成功内联的图片数
    skipped: int = 0                        # 已是 data: URI，原样保留
    missing: list[str] = field(default_factory=list)   # 解析不到本地文件的 src
    failed: list[str] = field(default_factory=list)    # 读取/编码失败
    payload_bytes: int = 0                  # 追加的 base64 字符数
    oversized: int = 0                      # data URI 超过告警阈值的图片数
    max_uri_bytes: int = 0                  # 最长的单张 data URI 长度


def _esc(text: str) -> str:
    """HTML 属性值转义。"""
    return (text.replace("&", "&amp;").replace('"', "&quot;")
                .replace("<", "&lt;").replace(">", "&gt;"))


def _human(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


def _img_tag(uri: str, alt: str, title: str) -> str:
    """构造内联图片标签。alt/title 为空时省略对应属性。"""
    attrs = [f'src="{uri}"']
    if alt:
        attrs.append(f'alt="{_esc(alt)}"')
    if title:
        attrs.append(f'title="{_esc(title)}"')
    return f"<img {' '.join(attrs)} />"


def _data_uri(path: Path, ctx: ConvertCtx) -> str | None:
    """把本地图片文件编码为 base64 data URI；失败返回 None。"""
    # 非浏览器友好的格式（tiff 等）先转 PNG；该函数只是「Pillow 转 PNG」，
    # 与 docx 路径共用同一份 images/ 缓存。
    safe = path if path.suffix.lower() in _HTML_OK_IMG_EXTS \
        else _docx_safe_image(path, ctx)
    if safe is None:
        return None
    try:
        raw = safe.read_bytes()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"  读取图片失败 ({safe.name}): {exc}")
        return None
    if not raw:
        return None
    mime = _MIME_BY_EXT.get(safe.suffix.lower(), "image/png")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _split_fences(text: str) -> list[tuple[bool, str]]:
    """按围栏代码块切成 ``(是否代码块, 片段)``，拼接后与原文本完全一致。"""
    parts: list[tuple[bool, str]] = []
    buf: list[str] = []
    fence: str | None = None

    def flush(is_code: bool) -> None:
        if buf:
            parts.append((is_code, "\n".join(buf)))
            buf.clear()

    for line in text.split("\n"):
        m = _FENCE_RE.match(line)
        if fence is None:
            if m:
                flush(False)
                fence = m.group(1)[0]
            buf.append(line)
        else:
            buf.append(line)
            if m and m.group(1)[0] == fence:
                fence = None
                flush(True)
        # 注：进入/离开围栏时把围栏行本身也归入代码片段，保证原样保留
    flush(fence is not None)
    return parts


def _collect_ref_defs(text: str) -> dict[str, str]:
    """收集引用式定义 ``[ref]: url``。"""
    refmap: dict[str, str] = {}
    for line in text.split("\n"):
        m = _REF_DEF_RE.match(line)
        if m:
            refmap[m.group(1).strip().lower()] = m.group(2)
    return refmap


def _expand_ref_images(text: str, refmap: dict[str, str]) -> str:
    """把引用式图片 ``![alt][ref]`` 展开为 ``![alt](url)``，便于统一内联。

    只动图片（带 ``!``），不动引用式链接；未定义的 ref 原样保留。
    """
    if not refmap:
        return text

    def _sub(m: re.Match) -> str:
        label, ref = m.group(1), m.group(2)
        url = refmap.get((ref.strip() or label).strip().lower())
        return f"![{label}]({url})" if url else m.group(0)

    return _REF_IMG_USE_RE.sub(_sub, text)


class _Inliner:
    """单次转换的图片内联器：持有 data URI 缓存，避免同一图片重复读盘编码。"""

    def __init__(self, ctx: ConvertCtx, stats: PortableStats):
        self.ctx = ctx
        self.stats = stats
        self._uri_cache: dict[str, str] = {}
        # 默认把 SVG 栅格化为 PNG（兼容性优先）；置 True 则保留矢量。
        self.keep_svg = bool(getattr(ctx.settings, "md_portable_keep_svg", False))

    def replace(self, src: str, alt: str, title: str, original: str) -> str:
        """把一条图片引用替换为内联 ``<img>``；无法处理时返回原文。"""
        src = (src or "").strip()
        if not src:
            return original
        if src.startswith("data:"):
            self.stats.skipped += 1        # 幂等：已是内联图片
            return original

        uri = self._uri_cache.get(src)
        if uri is None:
            path = _resolve_image(src, self.ctx,
                                  rasterize_svg=not self.keep_svg)
            if path is None:
                self.stats.missing.append(src)
                return original
            uri = _data_uri(path, self.ctx)
            if uri is None:
                self.stats.failed.append(src)
                return original
            self._uri_cache[src] = uri

        self.stats.inlined += 1
        self.stats.payload_bytes += len(uri)
        self.stats.max_uri_bytes = max(self.stats.max_uri_bytes, len(uri))
        if len(uri) > _SINGLE_URI_WARN_BYTES:
            self.stats.oversized += 1
        return _img_tag(uri, alt, title)


def _transform_text(part: str, inliner: _Inliner,
                    refmap: dict[str, str]) -> str:
    """对一段非代码文本做图片内联（行内代码先藏起来，避免改到示例）。"""
    saved: list[str] = []

    def _stash(m: re.Match) -> str:
        saved.append(m.group(0))
        return f"⟦MDCODE{len(saved) - 1}⟧"

    work = _CODE_SPAN_RE.sub(_stash, part)
    work = _expand_ref_images(work, refmap)

    def _sub_html(m: re.Match) -> str:
        tag = m.group(0)
        sm = _SRC_ATTR_RE.search(tag)
        if not sm:
            return tag
        src = next((g for g in sm.groups() if g is not None), "")
        am = _ALT_ATTR_RE.search(tag)
        tm = _TITLE_ATTR_RE.search(tag)
        alt = next((g for g in am.groups() if g is not None), "") if am else ""
        title = next((g for g in tm.groups() if g is not None), "") if tm else ""
        return inliner.replace(src, alt, title, tag)

    def _sub_md(m: re.Match) -> str:
        src = m.group("src_angle") or m.group("src") or ""
        title = (m.group("title") or "").strip("\"'()")
        return inliner.replace(src, m.group("alt") or "", title, m.group(0))

    # 先处理 HTML 标签，再处理 markdown 语法：否则新插入的 <img> 会被再次扫到
    work = _HTML_IMG_RE.sub(_sub_html, work)
    work = _MD_IMG_RE.sub(_sub_md, work)

    return _PLACEHOLDER_RE.sub(lambda m: saved[int(m.group(1))], work)


def to_portable_markdown(text: str, ctx: ConvertCtx) -> tuple[str, PortableStats]:
    """把 markdown 源文本转为自包含 markdown，返回 (新文本, 统计)。"""
    stats = PortableStats()
    inliner = _Inliner(ctx, stats)
    refmap = _collect_ref_defs(text)
    parts = [
        part if is_code else _transform_text(part, inliner, refmap)
        for is_code, part in _split_fences(text)
    ]
    return "\n".join(parts), stats


def write_portable(text: str, out_path: Path, ctx: ConvertCtx) -> Path:
    """生成自包含 markdown 并写入 out_path（含结果日志与体积提示）。"""
    out_text, stats = to_portable_markdown(text, ctx)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(out_text, encoding="utf-8")
    size = out_path.stat().st_size

    logger.info(f"自包含 Markdown 已导出: {out_path}")
    logger.info(
        f"  内联图片 {stats.inlined} 张"
        f"（图片数据 +{_human(stats.payload_bytes)}，文件总大小 {_human(size)}）"
        + (f"；已是内联格式跳过 {stats.skipped} 张" if stats.skipped else "")
    )
    if stats.missing:
        logger.warning(
            f"  {len(stats.missing)} 张图片找不到本地文件，已保留原引用："
            + "、".join(stats.missing[:3])
            + ("…" if len(stats.missing) > 3 else "")
        )
    if stats.failed:
        logger.warning(
            f"  {len(stats.failed)} 张图片读取/编码失败，已保留原引用："
            + "、".join(stats.failed[:3])
            + ("…" if len(stats.failed) > 3 else "")
        )
    if stats.oversized:
        logger.warning(
            f"  {stats.oversized} 张图片的 data URI 超过 "
            f"{_SINGLE_URI_WARN_BYTES // 1024} KB"
            f"（最长 {_human(stats.max_uri_bytes)}）："
            "部分渲染器（实测 Typora 256 KB 起）会拒绝渲染，把整段 <img> 显示成"
            "纯文本；图片本身有效，用 VS Code / Obsidian / 浏览器打开通常正常。"
            "若必须发给这类渲染器，可先压缩图片再转换"
        )
    if size > _SIZE_WARN_BYTES:
        logger.warning(
            f"  文件达 {_human(size)}：base64 会让体积增大约 1/3，"
            "部分聊天工具/邮件可能拒绝或预览卡顿"
        )
    return out_path
