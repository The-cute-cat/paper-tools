"""逐页渲染、插图裁剪及带跨页状态的视觉提取。"""

import base64
from collections import Counter
from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

import pymupdf
import yaml
from openai import OpenAI

from ...config import AppSettings
from ...logging_setup import get_logger

IMAGE_RE = re.compile(r"\[\[IMAGE:(p\d{4,}_img\d{3,})\]\]")

# ---------- 提示词模板：从 YAML 加载，与 core/translator_prompts.yaml 同一套做法 ----------
_PROMPT_PATH = Path(__file__).resolve().parent / "extractor_prompts.yaml"


@lru_cache(maxsize=1)
def _load_prompts() -> dict:
    """加载视觉提取提示词 YAML 模板（模块级单例缓存）。"""
    with open(_PROMPT_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _build_extraction_prompt() -> str:
    """拼装系统提示词：intro + 编号规则 + 输出格式（与 _build_system 同一布局）。"""
    prompts = _load_prompts()
    parts = [prompts["intro"], *prompts["rules"], "", prompts["output"]]
    return "\n".join(parts)


PROMPT = _build_extraction_prompt()


def atomic_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def file_digest(source: Path, *, length: int | None = None) -> str:
    """计算文件 SHA-256（十六进制）。length 用于截断前缀（如目录名用 12 位）。"""
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return digest[:length] if length else digest


@dataclass
class PageResult:
    complete: str
    carry: str
    ignored_images: list[str]


def validate_result(data: object, current_ids: list[str], carry: str,
                    last_page: bool) -> PageResult:
    if not isinstance(data, dict):
        raise ValueError("识别输出必须是 JSON 对象")
    if not all(isinstance(data.get(k), str) for k in ("complete", "carry")):
        raise ValueError("识别输出缺少 complete/carry 字符串")
    ignored = data.get("ignored_images")
    if not isinstance(ignored, list) or not all(isinstance(x, str) for x in ignored):
        raise ValueError("ignored_images 必须是编号数组")
    if len(set(ignored)) != len(ignored) or not set(ignored) <= set(current_ids):
        raise ValueError("忽略了重复或未知图片编号")
    combined = data["complete"] + "\n" + data["carry"]
    expected = Counter(current_ids + IMAGE_RE.findall(carry))
    actual = Counter(IMAGE_RE.findall(combined)) + Counter(ignored)
    if actual != expected:
        raise ValueError("图片编号丢失、重复或出现未知编号")
    if "[[IMAGE:" in IMAGE_RE.sub("", combined):
        raise ValueError("图片占位符格式错误")
    if last_page and data["carry"].strip():
        raise ValueError("最后一页仍有未归档的跨页内容")
    if carry.strip() and not combined.strip():
        raise ValueError("跨页内容被清空")
    return PageResult(data["complete"], data["carry"], ignored)


def figure_rects(page: pymupdf.Page) -> list[pymupdf.Rect]:
    # 裁剪页面可保留透明蒙版、复合图和矢量线条，而非只导出裸 xref。
    rects = [pymupdf.Rect(i["bbox"]) for i in page.get_image_info()]
    rects.extend(page.cluster_drawings())
    merged: list[pymupdf.Rect] = []
    for rect in rects:
        rect = rect & page.rect
        if rect.is_empty or rect.width < 8 or rect.height < 8:
            continue
        if rect.get_area() > page.rect.get_area() * .90:
            get_logger().warning("第 %d 页存在整页图像/背景，无法按对象拆分其中插图", page.number + 1)
            continue
        # 重复/交叠区域合并，直到无传递交叠。
        i = 0
        while i < len(merged):
            if (rect + (-2, -2, 2, 2)).intersects(merged[i]):
                rect |= merged.pop(i)
                i = 0
            else:
                i += 1
        merged.append(rect)
    return sorted(merged, key=lambda r: (r.y0, r.x0))


def render_page(page: pymupdf.Page, root: Path, dpi: int) -> tuple[list[Path], dict[str, Path]]:
    # 各坐标 API 返回未旋转坐标，统一去除 rotation 后渲染与裁剪。
    page.set_rotation(0)
    name = f"p{page.number + 1:04d}"
    page_path = root / "pages" / f"{name}.png"
    def render(path: Path, clip=None):
        rect = clip or page.rect
        scale = min(dpi / 72, 4000 / max(rect.width, rect.height))
        page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip,
                        colorspace=pymupdf.csRGB, alpha=False).save(path)
    render(page_path)
    views = [page_path]
    # 视觉模型会缩小每张输入图；四个重叠分块让小字仍可见。
    for i, (x, y) in enumerate(((0, 0), (.45, 0), (0, .45), (.45, .45)), 1):
        r = page.rect
        clip = pymupdf.Rect(r.x0 + x*r.width, r.y0 + y*r.height,
                            r.x0 + (x+.55)*r.width, r.y0 + (y+.55)*r.height)
        path = root / "pages" / f"{name}_detail{i}.png"
        render(path, clip)
        views.append(path)
    assets = {}
    for i, rect in enumerate(figure_rects(page), 1):
        ident = f"{name}_img{i:03d}"
        path = root / "images" / f"{ident}.png"
        render(path, rect)
        assets[ident] = path
    return views, assets


class VisionExtractor:
    def __init__(self, settings: AppSettings):
        self.settings = settings
        self.client = OpenAI(api_key=settings.llm.api_key, base_url=settings.llm.base_url,
                             timeout=settings.llm.timeout, max_retries=settings.llm.max_retries)

    def close(self):
        self.client.close()

    def recognize(self, views: list[Path], assets: dict[str, Path], carry: str,
                  last_page: bool) -> PageResult:
        content = [{"type": "text", "text": json.dumps(
            {"carry": carry, "last_page": last_page}, ensure_ascii=False)}]
        inputs = [("整页" if i == 0 else f"阅读细节{i}", p) for i, p in enumerate(views)]
        inputs.extend(assets.items())
        # 超限提示统一带上"已完成的页已缓存"的说明：这些限制在第 N 页才可能触发，
        # 此前付费识别的页面都已落盘，降低 DPI 后重新运行即可从断点继续。
        hint = "；已完成的页已缓存，调整后重新运行可续跑"
        if len(inputs) > 600:
            raise ValueError(f"本页图片超过接口 600 张限制，请降低 DPI{hint}")
        for label, path in inputs:
            raw = path.read_bytes()
            if len(raw) > 32 * 1024**2:
                raise ValueError(f"单张图像超过 32 MiB，请降低 DPI{hint}")
            content.extend([{"type": "text", "text": label},
                            {"type": "image_url", "image_url": {
                                "url": "data:image/png;base64," + base64.b64encode(raw).decode("ascii"),
                                "detail": "high"}}])
        if len(json.dumps(content).encode()) > 47 * 1024**2:
            raise ValueError(f"本页请求接近 48 MiB 上限，请降低 DPI{hint}")
        error = ""
        for attempt in range(self.settings.llm.max_retries + 1):
            response = self.client.chat.completions.create(
                model=self.settings.pdf_vision_model,
                messages=[{"role": "system", "content": PROMPT + error},
                          {"role": "user", "content": content}],
                response_format={"type": "json_object"}, temperature=0,
                max_tokens=self.settings.pdf_max_output_tokens,
            )
            try:
                choice = response.choices[0]
                if choice.finish_reason != "stop":
                    # length = 输出被 max_tokens 截断。重试不会改变上限，必须提示调大
                    # PAPER_TOOLS_PDF_MAX_TOKENS（降低 DPI 对输出长度无效）。
                    detail = ""
                    if choice.finish_reason == "length":
                        detail = (f"，本页内容超出输出上限 "
                                  f"{self.settings.pdf_max_output_tokens} token，"
                                  f"请调大 PAPER_TOOLS_PDF_MAX_TOKENS"
                                  f"（降低 DPI 无效，它不减少输出长度）")
                    raise ValueError(f"识别响应未完整结束: {choice.finish_reason}{detail}")
                return validate_result(json.loads(choice.message.content or ""),
                                       list(assets), carry, last_page)
            except (ValueError, IndexError) as exc:
                # 最后一次尝试仍失败：直接抛出具体原因（而不是笼统的"重试耗尽"），
                # 便于用户判断该调大输出上限、降 DPI 还是更换模型。
                if attempt == self.settings.llm.max_retries:
                    raise ValueError(str(exc)) from exc
                error = f"\n上次输出未通过校验：{exc}。请重新完整识别。"


def extract_pdf(source: Path, root: Path, settings: AppSettings, *, resume: bool = True,
                extractor=None, digest: str | None = None) -> Path:
    if not 72 <= settings.pdf_dpi <= 300:
        raise ValueError("PDF DPI 必须在 72-300 之间")
    for folder in ("pages", "images", "extraction"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    # 完整哈希用于逐页缓存 key；调用方（pipeline.run）已算过时直接复用，
    # 避免大 PDF 全文件重复读取。
    if digest is None:
        digest = file_digest(source)
    owned = extractor is None
    complete, carry, mapping = [], "", {}
    try:
        with pymupdf.open(source) as doc:
            if not doc.is_pdf or doc.needs_pass or not len(doc):
                raise ValueError("需要未加密、非空的有效 PDF 文件")
            extractor = extractor or VisionExtractor(settings)
            for page in doc:
                get_logger().info("阶段 1/2：识别 PDF 第 %d/%d 页", page.number + 1, len(doc))
                views, assets = render_page(page, root, settings.pdf_dpi)
                mapping.update({k: f"images/{p.name}" for k, p in assets.items()})
                last = page.number == len(doc) - 1
                key = hashlib.sha256(json.dumps([digest, page.number, carry,
                    settings.pdf_dpi, settings.pdf_vision_model, settings.llm.base_url, PROMPT,
                    settings.pdf_max_output_tokens, list(assets)],
                    ensure_ascii=False).encode()).hexdigest()
                cache = root / "extraction" / f"p{page.number + 1:04d}.json"
                result = None
                if resume and cache.exists():
                    try:
                        saved = json.loads(cache.read_text(encoding="utf-8"))
                        if saved["key"] == key:
                            result = validate_result(saved["result"], list(assets), carry, last)
                    except (ValueError, KeyError, TypeError):
                        pass
                if result is None:
                    result = extractor.recognize(views, assets, carry, last)
                    # 也校验自定义提取器输出，避免坏状态写入断点。用 asdict 而非
                    # vars()：不依赖返回对象具有 __dict__（如 namedtuple 会炸）。
                    result_data = asdict(result) if isinstance(result, PageResult) else dict(vars(result))
                    validate_result(result_data, list(assets), carry, last)
                    atomic_text(cache, json.dumps({"key": key, "input_carry": carry,
                                "result": result_data}, ensure_ascii=False, indent=2))
                complete.append(result.complete.strip())
                carry = result.carry
    finally:
        if owned and extractor is not None:
            extractor.close()
    md = "\n\n".join(x for x in complete if x)
    md = IMAGE_RE.sub(lambda m: f"![{m[1]}]({mapping[m[1]]})", md)
    path = root / f"{source.stem}.extracted.md"
    atomic_text(path, md + "\n")
    atomic_text(root / "images.json", json.dumps(mapping, ensure_ascii=False, indent=2))
    return path
