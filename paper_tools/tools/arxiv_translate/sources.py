"""论文来源识别与抽象。

当前支持两类来源：

  * **arXiv**    —— ar5iv / LaTeXML 生成的 HTML（``https://arxiv.org/abs/<id>``）。
  * **Springer** —— Springer Nature Link 文章页（``https://link.springer.com/article/<doi>``）。

每个来源封装：输出标识（``key``）、原文链接、可下载 HTML 地址、解析器。
翻译流水线本身与来源无关，只依赖这里产出的 :class:`Source`。

另外支持直接传入**本地保存的 HTML 文件**：link.springer.com 对脚本化请求返回
JS 反爬挑战页（"Client Challenge"），此时可在浏览器中「另存为」网页后传入本地
文件路径，工具会自动识别来源并解析。
"""

import re
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Callable, Optional

from bs4 import BeautifulSoup

from ...config import get_settings
from ...logging_setup import get_logger

logger = get_logger()

ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5}(v\d+)?)", re.IGNORECASE)
DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s\"'<>?#]+)", re.IGNORECASE)
SPRINGER_HOST_RE = re.compile(
    r"(?:link\.springer\.com|www\.springer\.com|springer\.com)/article/", re.I)
# DOI 形式的输入（裸 DOI / doi.org 链接）在本工具内按 Springer 处理
# （目前只支持 Springer 一家出版社的文章页）。
DOI_INPUT_RE = re.compile(r"(?:^|/)10\.\d{4,9}/", re.I)

# link.springer.com 的 JS 反爬挑战页特征。
_CHALLENGE_MARKERS = ("Client Challenge", "_fs-ch-", "Please enable JavaScript to proceed")


def parse_arxiv_id(url_or_id: str) -> str:
    """从 arxiv 链接或 ID 中提取 arxiv ID（含版本号）。"""
    m = ARXIV_ID_RE.search(url_or_id)
    if not m:
        raise ValueError(f"无法从输入中识别 arxiv ID: {url_or_id}")
    return m.group(1)


def resolve_arxiv_html_url(arxiv_id: str) -> tuple[str, str]:
    """解析 arxiv HTML 全文页面地址。

    arxiv 的 HTML 版本化资源位于 ``https://arxiv.org/html/<id>vN``，
    不带版本号的根路径 ``/html/<id>`` 在部分论文上会 404。为稳定获取
    "最新版本" 的 HTML，这里统一访问 abs 摘要页
    ``https://arxiv.org/abs/<id>``，解析其中指向 ``/html/`` 的链接，
    取版本号最大的那个作为 HTML 全文地址。

    返回 (html_url, base_url)：
      - html_url：可下载的 HTML 全文完整 URL（含版本号）。
      - base_url：该 HTML 文档根（用于补全相对图片路径），如
        ``https://arxiv.org/html/``。
    若 abs 页解析失败，回退为直接构造 ``/html/<arxiv_id>``。
    """
    import requests

    base_id = re.sub(r"v\d+$", "", arxiv_id, flags=re.IGNORECASE)
    abs_url = f"https://arxiv.org/abs/{base_id}"
    try:
        from ...core.downloader import _text_request
        fetch_url, proxies = _text_request(abs_url)
        resp = requests.get(fetch_url, timeout=get_settings().download_timeout,
                            headers=get_settings().download_headers, proxies=proxies)
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
            # ``https://arxiv.org/html/<id>vN/<file>``，故文档根取站点根。
            logger.info(f"从 abs 页解析到最新 HTML 版本: {html_url}")
            return html_url, "https://arxiv.org/html/"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"解析 abs 页获取 HTML 链接失败，回退直接构造: {e}")

    return f"https://arxiv.org/html/{arxiv_id}", "https://arxiv.org/html/"


def _parse_arxiv(html_path, img_mapping, base_url):
    from .parser import parse_arxiv_html
    return parse_arxiv_html(html_path, img_mapping=img_mapping, base_url=base_url)


def _parse_springer(html_path, img_mapping, base_url, *, article_url=None):
    from ...core.downloader import download_text
    from .springer import parse_springer_html
    return parse_springer_html(html_path, img_mapping=img_mapping, base_url=base_url,
                               article_url=article_url, table_fetcher=download_text)


def _validate_springer_html(html_text: str) -> None:
    """检测 Springer 的 JS 反爬挑战页，给出可操作的提示。"""
    if any(m in html_text for m in _CHALLENGE_MARKERS):
        raise RuntimeError(
            "link.springer.com 返回了 JS 反爬挑战页（Client Challenge），无法用普通 HTTP 抓取。\n"
            "  解决办法（任选其一）：\n"
            "    1. 配置 CORS 转发代理（推荐，服务端代抓可绕过挑战）：在 .env 设置\n"
            "       PAPER_TOOLS_CORS_PROXY=https://你的worker.workers.dev/?url=\n"
            "    2. 浏览器打开文章页 -> 另存为「网页, 仅 HTML」-> 把本地 .html 路径作为输入传入。"
        )
    if "c-article-body" not in html_text and "c-article-title" not in html_text:
        logger.warning("Spring 页面中未找到 c-article-body，结构可能已变化，解析结果可能不完整")


def _doi_slug(doi: str) -> str:
    """DOI → 安全的文件/目录名（``10.1007/s10506-...`` → ``10.1007_s10506-...``）。"""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", doi).strip("_")


@dataclass
class Source:
    """一个可翻译的论文来源。"""
    kind: str                       # "arxiv" | "springer"
    key: str                        # 输出目录 / 文件基名
    origin_url: str                 # 用于头部「原文」链接
    display_name: str               # "arXiv" / "Springer"
    base_url: str = ""              # 相对链接 / 图片补全基准
    html_url: str = ""              # 待下载的 HTML 地址
    local_html: Optional[Path] = None
    parser: Optional[Callable] = None
    resolve: Optional[Callable[[], tuple[str, str]]] = None
    validate_html: Optional[Callable[[str], None]] = None
    meta: dict = field(default_factory=dict)

    def resolve_html(self) -> tuple[str, str]:
        """返回 (html_url, base_url)。"""
        if self.resolve is not None:
            return self.resolve()
        return self.html_url, self.base_url


def _sniff_local_html(path: Path) -> tuple[str, str]:
    """嗅探本地 HTML 的来源与标识，返回 (kind, key)。"""
    head = path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(head, "html.parser")
    if "c-article-body" in head or "c-article-title" in head or \
            soup.find("meta", attrs={"name": "citation_doi"}) is not None:
        doi_meta = soup.find("meta", attrs={"name": "citation_doi"})
        doi = (doi_meta.get("content") if doi_meta else "") or ""
        return "springer", (_doi_slug(doi) if doi else path.stem)
    return "arxiv", path.stem


def _source_from_local_html(path: Path) -> Source:
    kind, key = _sniff_local_html(path)
    if kind == "springer":
        soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "html.parser")
        doi_meta = soup.find("meta", attrs={"name": "citation_doi"})
        doi = (doi_meta.get("content") if doi_meta else "") or ""
        origin = f"https://doi.org/{doi}" if doi else ""
        article_url = f"https://link.springer.com/article/{doi}" if doi else ""
        return Source(kind="springer", key=key, origin_url=origin, display_name="Springer",
                      base_url="https://link.springer.com/", local_html=path,
                      parser=partial(_parse_springer, article_url=article_url))
    return Source(kind="arxiv", key=key, origin_url="", display_name="arXiv",
                  local_html=path, parser=_parse_arxiv)


def detect_source(url_or_id: str) -> Source:
    """根据输入识别来源（URL / ID / 本地 HTML 文件）。"""
    raw = (url_or_id or "").strip()
    if not raw:
        raise ValueError("输入为空：请提供 arXiv / Springer 链接、DOI、或本地 HTML 文件路径")

    # 1) 本地 HTML 文件
    p = Path(raw)
    if p.is_file() and p.suffix.lower() in (".html", ".htm"):
        logger.info(f"检测到本地 HTML 文件输入: {p}")
        return _source_from_local_html(p)

    # 2) Springer Nature Link 文章页（也接受裸 DOI / doi.org 链接）
    m = DOI_RE.search(raw)
    if m and (SPRINGER_HOST_RE.search(raw) or DOI_INPUT_RE.search(raw) or
              not raw.lower().startswith("http")):
        doi = m.group(1)
        article_url = f"https://link.springer.com/article/{doi}"
        return Source(
            kind="springer", key=_doi_slug(doi), origin_url=article_url,
            display_name="Springer", base_url="https://link.springer.com/",
            html_url=article_url, parser=partial(_parse_springer, article_url=article_url),
            validate_html=_validate_springer_html,
        )

    # 3) arXiv（默认）
    arxiv_id = parse_arxiv_id(raw)
    base_id = re.sub(r"v\d+$", "", arxiv_id, flags=re.IGNORECASE)
    return Source(
        kind="arxiv", key=arxiv_id, origin_url=f"https://arxiv.org/abs/{base_id}",
        display_name="arXiv", base_url="https://arxiv.org/html/",
        resolve=lambda: resolve_arxiv_html_url(arxiv_id), parser=_parse_arxiv,
    )
