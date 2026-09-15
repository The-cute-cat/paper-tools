"""逐页渲染、插图裁剪及带跨页状态的视觉提取。"""

import base64
from collections import Counter
from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import time
import unicodedata

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


class OutputLimitError(ValueError):
    """模型输出达到上限；相同参数重试不会成功。"""


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


def _carry_key(text: str) -> str:
    """用于守恒检查的宽松文本指纹。

    Markdown / LaTeX 标点和排版断词连字符不参与比较，因此 ``transi-`` 可以由
    下一页正确接成 ``transitions``；自然语言字母、数字和中日韩文字不能凭空消失。
    图片编号另由严格计数器检查。
    """
    text = IMAGE_RE.sub("", text)
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(char for char in text if char.isalnum())


def _repair_numbered_headings(text: str) -> str:
    """修复视觉模型偶尔漏掉的数字章节 Markdown 标记。"""
    pattern = re.compile(
        r"(?m)^(?!\s*[#|>\-*])(?P<number>\d+(?:\.\d+){0,4})\.?\s+"
        r"(?P<title>[^\n]{2,140})[ \t]*$"
    )

    def replace(match: re.Match) -> str:
        title = match["title"].strip()
        # 只处理明显的标题式短行，避免把有序列表或普通编号句变成章节。
        words = re.findall(r"[A-Za-z][A-Za-z'-]*", title)
        significant = [word for word in words if len(word) > 2]
        title_case = sum(word[0].isupper() for word in significant)
        if not significant or title_case / len(significant) < 0.55:
            return match[0]
        level = min(6, match["number"].count(".") + 2)
        return f"{'#' * level} {match['number']} {title}"

    return pattern.sub(replace, text)


def _stitch_leading_carry(complete: str, new_carry: str, carry: str) -> str:
    """确定性接回被模型漏掉、且显然由页首小写单词续写的 carry。"""
    if not carry.strip() or IMAGE_RE.search(carry):
        return complete
    prefix = complete[:len(carry) + 2500] + "\n" + new_carry
    if _carry_key(carry) and _carry_key(carry) in _carry_key(prefix):
        return complete
    leading = complete.lstrip()
    if not re.match(r"[a-z]", leading):
        return complete
    left = carry.rstrip()
    if re.search(r"[^\W\d_]{2,}-$", left, re.UNICODE):
        stitched = left[:-1] + leading
    else:
        stitched = left + " " + leading
    get_logger().warning("模型漏掉页首 carry，已按小写续词规则自动拼接")
    return stitched


def validate_result(data: object, current_ids: list[str], carry: str,
                    last_page: bool, page_text_hint: str = "") -> PageResult:
    if not isinstance(data, dict):
        raise ValueError("识别输出必须是 JSON 对象")
    if not all(isinstance(data.get(k), str) for k in ("complete", "carry")):
        raise ValueError("识别输出缺少 complete/carry 字符串")
    ignored = data.get("ignored_images")
    if not isinstance(ignored, list) or not all(isinstance(x, str) for x in ignored):
        raise ValueError("ignored_images 必须是编号数组")
    duplicate_ignored = sorted(
        ident for ident, count in Counter(ignored).items() if count > 1
    )
    unknown_ignored = sorted(set(ignored) - set(current_ids))
    if duplicate_ignored or unknown_ignored:
        raise ValueError(
            "ignored_images 编号非法；"
            f"重复={duplicate_ignored or '无'}；未知={unknown_ignored or '无'}；"
            f"本页合法编号={current_ids or '无'}"
        )
    data["complete"] = _stitch_leading_carry(
        data["complete"], data["carry"], carry
    )
    data["complete"] = _repair_numbered_headings(data["complete"])
    data["carry"] = _repair_numbered_headings(data["carry"])
    combined = data["complete"] + "\n" + data["carry"]
    expected = Counter(current_ids + IMAGE_RE.findall(carry))
    actual = Counter(IMAGE_RE.findall(combined)) + Counter(ignored)
    if actual != expected:
        missing = list((expected - actual).elements())
        excess = list((actual - expected).elements())
        raise ValueError(
            "图片编号守恒失败；"
            f"缺失={missing or '无'}；重复或未知={excess or '无'}；"
            "每个缺失编号必须在 complete/carry 中以 [[IMAGE:编号]] 出现一次，"
            "或在 ignored_images 中出现一次"
        )
    if "[[IMAGE:" in IMAGE_RE.sub("", combined):
        raise ValueError("图片占位符格式错误")
    if last_page and data["carry"].strip():
        raise ValueError("最后一页仍有未归档的跨页内容")
    carry_key = _carry_key(carry)
    if carry_key:
        # 跨页续文必须位于下一页输出的前部；仅为页顶浮动图及图注预留有限空间。
        # 不能搜索整页，否则短 carry（如 "moderate"）可能被后文表注中的同词
        # 偶然命中，掩盖真实的跨页丢字。
        prefix_chars = len(carry) + 2500
        output_key = _carry_key(data["complete"][:prefix_chars] + "\n" + data["carry"])
        if carry_key not in output_key:
            raise ValueError("上页 carry 文本未被完整保留")
    # 带文字层的论文通常可以可靠提取独立公式编号，用它弥补视觉模型容易漏掉
    # 页边小号 (1)/(2) 的弱点。接受 Markdown 文本编号或 LaTeX \tag{n}。
    equation_labels = set(re.findall(r"\(\s*(\d+[a-z]?)\s*\)", page_text_hint))
    missing_labels = [label for label in sorted(equation_labels)
                      if not re.search(
                          rf"(?:\(\s*{re.escape(label)}\s*\)|"
                          rf"\\tag\{{\s*{re.escape(label)}\s*\}})", combined)]
    if missing_labels:
        raise ValueError(f"公式编号缺失: {', '.join(missing_labels)}")
    if not last_page and not data["carry"].strip():
        tail = re.sub(r"\s+", "", data["complete"])
        if re.search(r"[^\W\d_]{2,}-$", tail, re.UNICODE):
            raise ValueError("complete 以排版断词连字符结尾，应归入 carry 等待下一页")
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
                  last_page: bool, page_text_hint: str = "") -> PageResult:
        content = [{"type": "text", "text": json.dumps(
            {"carry": carry, "last_page": last_page,
             "pdf_text_hint": page_text_hint}, ensure_ascii=False)}]
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
        previous_raw = ""
        for attempt in range(self.settings.llm.max_retries + 1):
            messages = [{"role": "system", "content": PROMPT},
                        {"role": "user", "content": content}]
            if previous_raw:
                # 把失败版本交还给模型做定点修复。仍保留原始图片消息，模型可重新
                # 判断遗漏候选应插回正文还是加入 ignored_images，而无需从零猜测。
                messages.extend([
                    {"role": "assistant", "content": previous_raw},
                    {"role": "user", "content": (
                        "上一版 JSON 的正文尽量原样保留，只修复以下校验问题并输出"
                        "一份完整的新 JSON。不得解释，不得只输出差异：\n" + error
                    )},
                ])
            get_logger().info(
                "发送视觉识别请求（尝试 %d/%d，图片 %d 张，正文候选 %d 个）",
                attempt + 1, self.settings.llm.max_retries + 1,
                len(inputs), len(assets),
            )
            request = dict(
                model=self.settings.pdf_vision_model,
                messages=messages,
                response_format={"type": "json_object"}, temperature=0,
                max_tokens=self.settings.pdf_max_output_tokens,
            )
            # DeepSeek V4 默认启用思考模式；视觉转录是机械任务，开启思考不仅
            # 拖慢响应，还会让推理 token 与正文争用 max_tokens。
            if "deepseek" in self.settings.llm.base_url.lower():
                request["extra_body"] = {"thinking": {"type": "disabled"}}
            started = time.monotonic()
            response = self.client.chat.completions.create(**request)
            get_logger().info(
                "视觉模型已响应，耗时 %.1f 秒，开始校验", time.monotonic() - started
            )
            try:
                choice = response.choices[0]
                if choice.finish_reason == "length":
                    # 相同参数重试必然再次碰到同一上限，立即失败，避免一页等待
                    # (max_retries + 1) 倍时间并重复计费。
                    hint_chars = len(page_text_hint)
                    raise OutputLimitError(
                        f"识别响应未完整结束: length，本页输出达到 "
                        f"{self.settings.pdf_max_output_tokens} token 上限"
                        f"（PDF 文字层约 {hint_chars} 字符，可能是模型异常重复输出）"
                        f"；已停止无效重试。可重跑本页，若仍发生再调大 "
                        f"PAPER_TOOLS_PDF_MAX_TOKENS；降低 DPI 无效"
                    )
                if choice.finish_reason != "stop":
                    raise ValueError(f"识别响应未完整结束: {choice.finish_reason}")
                previous_raw = choice.message.content or ""
                return validate_result(json.loads(previous_raw),
                                       list(assets), carry, last_page, page_text_hint)
            except (ValueError, IndexError) as exc:
                if isinstance(exc, OutputLimitError):
                    raise
                # 最后一次尝试仍失败：直接抛出具体原因（而不是笼统的"重试耗尽"），
                # 便于用户判断该调大输出上限、降 DPI 还是更换模型。
                if attempt == self.settings.llm.max_retries:
                    raise ValueError(str(exc)) from exc
                error = str(exc)
                get_logger().warning(
                    "视觉结果校验失败，将定点修正（尝试 %d/%d）：%s",
                    attempt + 1, self.settings.llm.max_retries + 1, error,
                )


# 视觉模型更名兼容：官方说明旧模型名 deepseek-v4-flash-vision-exp 的请求仍由
# DeepSeek-V4.1-Flash（即 deepseek-flash）提供服务，因此更名前已缓存的逐页识别
# 结果依然有效，不应因改名而全部重算。
_VISION_MODEL_ALIASES = {
    "deepseek-flash": "deepseek-v4-flash-vision-exp",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
}


def _extraction_cache_keys(digest: str, page_number: int, carry: str,
                           settings: AppSettings, page_text_hint: str,
                           asset_ids: list[str]) -> tuple[str, set[str]]:
    """返回当前缓存键及可兼容的旧键。

    三类兼容：

    * max_tokens 只限制失败响应，不影响一份已经完整并通过校验的结果，因此不应
      让调大上限导致前面所有成功页面失效（兼容参与哈希的旧版 16384 键）；
    * 视觉模型更名（见 ``_VISION_MODEL_ALIASES``）：实际服务方是同一模型，因此
      更名前用旧模型名写入的键也一并接受，避免整本 PDF 重算；
    * 旧版无 ``v2`` 前缀、但 max_tokens 参与哈希的键。
    """
    def key_of(model_name: str, limit: int | None = None,
               versioned: bool = True) -> str:
        if versioned:
            payload: list = ["v2", digest, page_number, carry, settings.pdf_dpi,
                             model_name, settings.llm.base_url, PROMPT,
                             page_text_hint, asset_ids]
        else:
            payload = [digest, page_number, carry, settings.pdf_dpi,
                       model_name, settings.llm.base_url, PROMPT,
                       limit, page_text_hint, asset_ids]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()

    model_names = {settings.pdf_vision_model}
    if alias := _VISION_MODEL_ALIASES.get(settings.pdf_vision_model):
        model_names.add(alias)

    current = key_of(settings.pdf_vision_model)
    legacy: set[str] = set()
    for model_name in model_names:
        legacy.add(key_of(model_name))
        for limit in {settings.pdf_max_output_tokens, 16384}:
            legacy.add(key_of(model_name, limit, versioned=False))
    legacy.discard(current)
    return current, legacy


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
                # 文字层只作为视觉转录的校对线索：可补足公式编号、专名拼写、作者单位，
                # 但其双栏顺序和公式通常不可靠，最终版式仍以页面图为准。
                page_text_hint = page.get_text("text", sort=True)
                mapping.update({k: f"images/{p.name}" for k, p in assets.items()})
                last = page.number == len(doc) - 1
                key, legacy_keys = _extraction_cache_keys(
                    digest, page.number, carry, settings, page_text_hint,
                    list(assets)
                )
                cache = root / "extraction" / f"p{page.number + 1:04d}.json"
                result = None
                if resume and cache.exists():
                    try:
                        saved = json.loads(cache.read_text(encoding="utf-8"))
                        if saved["key"] == key or saved["key"] in legacy_keys:
                            result = validate_result(
                                saved["result"], list(assets), carry, last, page_text_hint
                            )
                            normalized = asdict(result)
                            # validate_result 可能原地修复标题或 carry；无条件以当前
                            # key 回写已规范化结果，避免每次启动重复同一修复和警告。
                            atomic_text(cache, json.dumps(
                                {"key": key, "input_carry": carry,
                                 "result": normalized},
                                ensure_ascii=False, indent=2,
                            ))
                            get_logger().info(
                                "复用第 %d 页识别缓存", page.number + 1
                            )
                    except (ValueError, KeyError, TypeError):
                        pass
                if result is None:
                    result = extractor.recognize(
                        views, assets, carry, last, page_text_hint=page_text_hint
                    )
                    # 也校验自定义提取器输出，避免坏状态写入断点。用 asdict 而非
                    # vars()：不依赖返回对象具有 __dict__（如 namedtuple 会炸）。
                    result_data = asdict(result) if isinstance(result, PageResult) else dict(vars(result))
                    validate_result(result_data, list(assets), carry, last, page_text_hint)
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
