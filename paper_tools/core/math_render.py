"""把 LaTeX 数学公式渲染为透明背景 PNG（供 Word / PDF 内嵌）。

实现：matplotlib 的 mathtext（纯 Python，无需系统 LaTeX 引擎）。
- 每个公式按内容哈希缓存到 out_dir，避免重复渲染。
- inline 公式用较小字号；display 公式用较大字号并单独成行。
- 渲染失败时回退返回 None（调用方退化为文本占位）。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import rcParams

# 使用 Computer Modern 字体集，最接近 LaTeX 排版观感
rcParams["mathtext.fontset"] = "cm"

# inline / display 渲染参数
_INLINE_FONTSIZE = 11
_DISPLAY_FONTSIZE = 13
_DPI = 200

# mathtext 不支持、但 arxiv 偶尔出现的宏的简单清洗
_DELETE_PATTERNS = [
    r"\\label\{[^}]*\}",          # 删除 \label{...}
    r"\\tag\{[^}]*\}",           # 删除 \tag{...}
    r"\\nonumber",               # 删除 \nonumber
    r"\\displaystyle",            # 删除 \displaystyle（mathtext 不支持）
]
# \begin{split}...\end{split} 等多行环境：剥离 begin/end，把 \\ 换行换成 ;
_BEGINENV_RE = re.compile(r"\\begin\{[a-zA-Z*]+\}")
_ENDENV_RE = re.compile(r"\\end\{[a-zA-Z*]+\}")
_NEWLINE_RE = re.compile(r"\\\\")
_STRIP_BRACE_RE = re.compile(r"\\[a-zA-Z]+\s*\{([^{}]*)\}")
_DELETE_RE = [re.compile(p) for p in _DELETE_PATTERNS]
_MACRO_MAP = {
    r"\bm": r"\mathbf",          # \bm -> \mathbf（mathtext 无 \bm）
    r"\boldsymbol": r"\mathbf",
    r"\mathbb{I}": r"\mathbf{I}", # \mathbb{I} -> \mathbf{I}（mathtext 无 \mathbb{I}）
    r"\texttt": r"\text",         # \texttt -> \text
    r"\textit": r"\text",         # \textit -> \text
    r"\textrm": r"\text",         # \textrm -> \text
    r"\mathrm": "",               # \mathrm{X} 近似默认
    r"\thicksim": r"\sim",         # \thicksim -> \sim（mathtext 不支持 \thicksim）
    r"\thickapprox": r"\approx",   # 同上
    r"\varpropto": r"\propto",
    r"\shortmid": r"|",
    r"\shortparallel": r"\|",
}
# 把独立的 & 替换为 \ \& （mathtext 中 & 不是合法文本）
_AMP_RE = re.compile(r"(?<!\\)&(?!&)")


def _clean_tex(tex: str) -> str:
    s = " ".join(tex.split())
    for r in _DELETE_RE:
        s = r.sub("", s)
    # 多行环境：剥离 \begin{...} / \end{...}，把换行 \\ 替换为 ;
    s = _BEGINENV_RE.sub("", s)
    s = _ENDENV_RE.sub("", s)
    s = _NEWLINE_RE.sub("; ", s)
    for k, v in _MACRO_MAP.items():
        s = s.replace(k, v)
    s = _AMP_RE.sub(r" \& ", s)
    # 去掉多余 $$ 包裹（mathtext 用 $...$ 或 r"..."）
    s = s.strip().strip("$").strip()
    return s


def render_math_to_png(tex: str, display: bool = False, out_dir: Path | None = None) -> Path | None:
    """渲染公式 latex 源码为透明 PNG，返回路径；失败返回 None。"""
    tex = (tex or "").strip()
    if not tex:
        return None
    clean = _clean_tex(tex)
    if not clean:
        return None

    out_dir = Path(out_dir) if out_dir else Path.cwd() / "math_cache"
    out_dir.mkdir(parents=True, exist_ok=True)

    key = hashlib.sha1(f"{'D' if display else 'I'}:{clean}".encode("utf-8")).hexdigest()[:16]
    ext = "display" if display else "inline"
    png = out_dir / f"math_{ext}_{key}.png"
    if png.exists():
        return png

    try:
        fontsize = _DISPLAY_FONTSIZE if display else _INLINE_FONTSIZE
        expr = rf"${clean}$" if not clean.startswith("$") else clean
        fig = plt.figure(figsize=(6, 1.2) if display else (4, 0.8))
        fig.text(0.01, 0.5, expr, fontsize=fontsize, va="center", ha="left")
        fig.savefig(
            png,
            dpi=_DPI,
            transparent=True,
            bbox_inches="tight",
            pad_inches=0.02,
        )
        plt.close(fig)
        if png.stat().st_size < 200:
            # 渲染异常（几乎空白）时回退
            png.unlink(missing_ok=True)
            return None
        return png
    except Exception:
        plt.close("all")
        return None


def math_png_size(png: Path, dpi: int = _DPI) -> tuple[float, float]:
    """返回图片像素尺寸 (w, h)。"""
    try:
        from PIL import Image

        with Image.open(png) as im:
            return im.size
    except Exception:
        return (0, 0)
