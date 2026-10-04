"""统一的配置管理。

配置优先级（高 -> 低）：
    1. 命令行参数 / 函数显式传参
    2. 环境变量（DEEPSEEK_API_KEY 等）
    3. 项目根目录 `.env` 文件
    4. 代码内默认值

所有配置集中在此，新增工具如需配置直接扩展 Settings 即可。
"""

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（pyproject.toml 所在目录）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 浏览器伪装请求头：arxiv 等站点会对明显的 bot UA（如 paper-tools/1.0）在 TLS
# 握手阶段直接重置连接（WinError 10054）。使用真实浏览器的 UA 与配套头字段，
# 让下载请求看起来像普通浏览器访问，避免被识别为爬虫而断连。
BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    # 只声明 gzip/deflate，避免服务器返回 brotli 时本机未装 brotli 解码库而报错
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


def _load_env() -> None:
    """加载 .env（若存在）。仅在首次调用时执行。"""
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)


# python-dotenv 会解码双引号值中的转义序列，Windows 反斜杠路径会被破坏：
# "D:\...\tests\x.pdf" 中的 \t 变成真实 TAB。此处把这些控制字符还原为
# 两字面转义形式，使路径恢复原义（Path 不解释转义，还原后可直接使用）。
_MANGLED_PATH_ESCAPES = {
    "\t": r"\t", "\n": r"\n", "\r": r"\r",
    "\f": r"\f", "\v": r"\v", "\b": r"\b", "\a": r"\a",
}


def _repair_mangled_path(value: str) -> str:
    """还原被 dotenv 双引号转义破坏的路径中的控制字符。"""
    if not value or not any(c in value for c in _MANGLED_PATH_ESCAPES):
        return value
    for ctrl, esc in _MANGLED_PATH_ESCAPES.items():
        value = value.replace(ctrl, esc)
    return value


def _clean_path_value(value: str) -> str:
    """清理环境变量里的路径值：去空白 + 剥离误写的包裹引号 + 还原转义破坏。

    用户可能把引号写进值里（如 ``PAPER_TOOLS_MD_INPUT='\\"D:\\\\a.md\\"'``），
    经 dotenv 转义解码后引号会成为路径的一部分，导致「文件不存在」。
    这里去掉成对包裹的引号（可能有多层），再修复控制字符。
    """
    v = (value or "").strip()
    while len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1].strip()
    return _repair_mangled_path(v)


@dataclass
class LLMSettings:
    """大模型（翻译）相关配置。"""
    provider: str = "deepseek"
    api_key: str = ""
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    temperature: float = 0.3
    timeout: int = 60
    max_retries: int = 3


@dataclass
class AppSettings:
    """全局应用配置。"""
    llm: LLMSettings = field(default_factory=LLMSettings)
    output_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "output")
    log_level: str = "INFO"
    # 下载相关
    download_timeout: int = 60
    download_max_retries: int = 3      # 二进制下载（图片等）失败重试次数
    download_headers: dict = field(default_factory=lambda: dict(BROWSER_HEADERS))
    # 下载代理（HTTP/HTTPS）：如 "http://127.0.0.1:7890"。
    # 留空表示直连。部分网络对 arxiv.org 等域名在 TLS 握手阶段直接 RST（WinError 10054），
    # 此时伪装请求头无法绕过，必须走代理/VPN 才能下载。requests 也兼容
    # HTTP_PROXY/HTTPS_PROXY 环境变量，此处为显式可配置项。
    download_proxy: str = ""
    # 轻量 CORS 转发代理（?url= 改写模式），如
    # "https://your-worker.workers.dev/?url="。仅用于**文本/HTML 下载**（abs 页 + 全文）。
    # 该模式把目标 URL 拼到 ?url= 后直接请求代理域名，由代理服务端代为 fetch
    # （部署在 Cloudflare 等可直连 arxiv 的网络侧），不经过 requests 的 CONNECT 隧道，
    # 因此适用于不支持 CONNECT 隧道、但有服务侧出网能力的轻量代理。
    # 注意：此类轻量代理通常只支持文本转发、**不支持二进制（图片）下载**，
    # 因此图片等二进制下载仍走 download_proxy（标准 CONNECT 代理）。
    # 留空表示文本下载也走 download_proxy / 直连。
    cors_proxy: str = ""
    # 图片：False = 引用保持原网络 URL（不下载）；True = 下载到本地并改为本地相对路径
    image_local: bool = False
    # 翻译相关
    translate_concurrency: int = 8     # 并发翻译线程数（0/1 表示单线程）
    translate_repair: bool = True      # 翻译后是否做一致性检查与返修
    # 翻译单元「目标长度」下限（字符，按纯文本长度）：相邻同类型文本块（段落/
    # 列表项/文本框）会被贪心凑成长度在 [merge_min_chars, merge_target_max] 区间
    # 的翻译单元，一起以 JSON 分块翻译，减少碎片、保持上下文连贯。
    # 单块长度 ≥ merge_target_max 时独立成单元（不强行拆分）。设为 0 关闭合并。
    merge_min_chars: int = 1000
    # 翻译单元「目标长度」上限（字符）。凑单元时累计长度达到该值即关闭当前单元。
    # 设为 0 表示不限制上限（仍受 merge_min_chars 触发关闭）。
    merge_target_max: int = 1500
    # 引用搜索引擎：google | bing | duckduckgo | semantic_scholar | arxiv
    #   默认 bing（国内可访问），Google 在国内常被拦截
    cite_search_engine: str = "bing"
    # 引用显示模式：short = 只显示作者年份（短链，论文名靠 hover 提示）
    #              title = 显示作者年份 + 完整论文名（信息全但占行宽）
    cite_display_mode: str = "short"
    # 输出文件命名方式（翻译后的 .zh.md / .glossary.json）：
    #   id       = 用 arxiv ID 命名（默认，如 2603.16192v1.zh.md）
    #   title    = 用「原论文英文标题」命名（非法符号自动换为等价中文符号）
    #   title_zh = 用「翻译后的中文标题」命名（非法符号自动换为等价中文符号）
    output_name_mode: str = "id"
    # Token 用量报告：翻译结束后在日志中输出总 token 消耗、缓存命中/未命中及其占比。
    # 默认关闭，避免控制台刷屏；可通过配置或环境变量 PAPER_TOOLS_TOKEN_REPORT 开启。
    token_report: bool = False
    # 价目表解析方式（DeepSeek 官方定价页 → 结构化价格）：
    #   ai  （默认）= 把页面正文交给 LLM 按固定 JSON schema 抽取。官方改版、模型
    #                 改名/改价、峰谷档位增删都无需改代码，并能从脚注里识别模型别名
    #                 （把旧模型名映射到当前计费模型）；LLM 失败时自动回落规则解析。
    #   rule       = 仅用本地规则（BeautifulSoup + 关键词/正则）解析，零成本、结果
    #                 确定，但官方一改列结构或文案措辞就可能失效。
    # 环境变量 PAPER_TOOLS_PRICING_PARSER 可覆盖。
    pricing_parser: str = "ai"
    # 导出格式：翻译完成后自动导出为哪些额外格式。
    # 可选值：docx、pdf、docx_pdf（等同于同时 docx+pdf）、all（docx+pdf）。
    # 留空表示不导出额外格式，只输出 .zh.md。
    # 环境变量 PAPER_TOOLS_EXPORT_FORMATS 可覆盖（逗号分隔，如 docx,pdf）。
    export_formats: str = ""
    # 是否仍输出 .zh.md（markdown）。默认 True：导出 docx/pdf 时也会保留 markdown。
    # 设为 False 可只产出 docx/pdf（例如 --no-md / PAPER_TOOLS_OUTPUT_MD=false）。
    output_markdown: bool = True
    # 跳过翻译：True 时不调用 LLM，仅解析并输出论文英文原文（用于只想要
    # 结构化原文 markdown 的场景）。环境变量 PAPER_TOOLS_SKIP_TRANSLATE 可覆盖。
    translate_skip: bool = False
    # 待翻译的 arxiv 链接或 ID（也可通过 main.py 的 INPUT 常量或命令行提供）。
    # 环境变量 PAPER_TOOLS_INPUT 可覆盖（空字符串表示未配置，回退到 INPUT 常量）。
    arxiv_input: str = ""
    # 生成「全局立场摘要」时送入 LLM 的摘要文本字符上限。
    # 0（默认）= 不截断，使用完整论文摘要；>0 = 超过该字符数则截断（极少数学术
    # 摘要极长时可设一个上限，避免无谓的 token 消耗）。
    summary_max_abstract_chars: int = 0
    # 断点续译模式：检测到上次异常退出的翻译缓存时如何处理。
    #   ask  （默认）= 在终端询问用户 恢复(r) / 重新翻译(n) / 退出(q)
    #   auto  = 自动恢复（无交互环境或 CI 下默认沿用缓存，跳过已翻译块）
    #   never = 总是从头翻译（忽略缓存，启动即删除）
    resume_mode: str = "ask"
    # 输出文件覆盖策略：目标 markdown/译文文件已存在时如何处理。
    #   ask    = 终端询问 覆盖/跳过/退出（默认；非交互终端自动退化为跳过）
    #   always = 直接覆盖
    #   never  = 不覆盖，跳过写入（保留现有文件）
    overwrite_mode: str = "ask"
    # PDF 第一阶段逐页识别用的视觉模型。须支持「图像理解」：deepseek-flash 支持，
    # deepseek-v4-pro 不支持。该值参与逐页缓存 key，改动会导致已有缓存页重算。
    # 环境变量 DEEPSEEK_VISION_MODEL 可覆盖。
    pdf_vision_model: str = "deepseek-flash"
    pdf_dpi: int = 160
    # 待翻译的本地 PDF 文件路径（PDF 工具专用，不支持下载 URL）。
    # 环境变量 PAPER_TOOLS_PDF_INPUT 可覆盖；为空时回退到 main.py 的 pdf_path 常量。
    pdf_input: str = ""
    # 仅提取：True 时 PDF 工具只做第一阶段逐页识别（.extracted.md），不翻译。
    # 注意第一阶段仍需调用视觉模型，并非离线模式。环境变量 PAPER_TOOLS_PDF_EXTRACT_ONLY 可覆盖。
    pdf_extract_only: bool = False
    # 视觉模型单页识别的最大输出 token 数。
    # 内容密集的页面（多公式/长表格）可能超出默认上限导致输出被截断，
    # 此时响应 finish_reason=length。程序会立即停止，不对同一上限做无效重试。
    # 可先重跑该页，若仍发生再调大本值（受模型上下文上限约束）；
    # 降低 DPI 无法解决，因为它不减少输出 token。
    pdf_max_output_tokens: int = 16384
    # 待转换的本地 Markdown 文件路径（md-export 工具专用）。
    # 环境变量 PAPER_TOOLS_MD_INPUT 可覆盖；为空时需在命令行/IDE 入口传入。
    md_input: str = ""
    # md-export 默认导出格式：docx、pdf、docx_pdf（等同同时导出两者）、portable、
    # all（等同 docx_pdf）。可用逗号组合，如 docx,portable。
    # 环境变量 PAPER_TOOLS_MD_EXPORT_FORMATS 可覆盖。
    md_export_formats: str = "docx_pdf"
    # portable 输出时是否保留 SVG 的矢量格式（默认 False = 栅格化为 PNG）。
    #   False = 兼容性优先：data:image/svg+xml 属于「活动内容」，带 HTML 消毒的
    #           渲染环境可能过滤掉它，栅格化为 PNG 后各处都能显示。
    #   True  = 清晰度优先：SVG 以矢量内联，放大不糊、线条锐利，适合确定接收方
    #           用 Typora / VS Code / Obsidian 这类浏览器内核预览的场景。
    # 注意：docx/pdf 输出无法嵌入 SVG，始终栅格化，本项只影响 portable。
    # 环境变量 PAPER_TOOLS_MD_PORTABLE_KEEP_SVG 可覆盖。
    md_portable_keep_svg: bool = False
    # md-export 中文正文字体（DOCX 写入 eastAsia 字体；PDF 按名称查找系统字体）。
    # 常见可选：宋体（默认）、微软雅黑、黑体、楷体、仿宋、等线。
    # 环境变量 PAPER_TOOLS_MD_FONT 可覆盖。
    md_export_font: str = "宋体"
    # md-export：图表围栏渲染为图片的后端。覆盖主流 Markdown 编辑器（Typora 等）
    # 会原生渲染的三种围栏：```mermaid、```flow（flowchart.js）、
    # ```sequence（js-sequence-diagrams）；后两者先转译为 Mermaid 再渲染。
    #   auto（默认）= 系统 PATH 上有 mermaid-cli（mmdc）就用本地渲染，否则用在线服务。
    #   local  = 仅本地 mmdc：完全离线、不外传内容；缺失时退回 npx（首次会联网下载）。
    #   online = 仅在线渲染服务（见 md_mermaid_endpoint）。
    #   off    = 关闭，图表代码块按普通代码块原样输出。
    # 环境变量 PAPER_TOOLS_MD_MERMAID 可覆盖。
    md_mermaid_renderer: str = "auto"
    # 在线图表渲染服务地址（默认 mermaid.ink 的 pako 接口）。
    # 注意：在线渲染会把图表源码发送到该第三方服务器；涉密/内网文档请改用 local 或 off。
    # 环境变量 PAPER_TOOLS_MD_MERMAID_ENDPOINT 可覆盖。
    md_mermaid_endpoint: str = "https://mermaid.ink"
    # 在线渲染输出宽度（像素）：越大越清晰、体积越大；0 = 用服务默认尺寸。
    # 环境变量 PAPER_TOOLS_MD_MERMAID_WIDTH 可覆盖。
    md_mermaid_width: int = 1600
    # 图表主题：default / neutral / dark / forest / base；留空用默认主题。
    # 环境变量 PAPER_TOOLS_MD_MERMAID_THEME 可覆盖。
    md_mermaid_theme: str = ""
    # 本地 mermaid-cli（mmdc）可执行文件路径；留空则自动在 PATH 查找，再退回 npx。
    # 环境变量 PAPER_TOOLS_MD_MERMAID_MMDC 可覆盖。
    md_mermaid_mmdc: str = ""

    def resolve(self) -> "AppSettings":
        """用环境变量/默认值补全缺失字段。"""
        _load_env()
        if not self.llm.api_key:
            self.llm.api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if env := os.environ.get("DEEPSEEK_MODEL"):
            self.llm.model = env
        if env := os.environ.get("DEEPSEEK_BASE_URL"):
            self.llm.base_url = env
        if env := os.environ.get("DEEPSEEK_VISION_MODEL"):
            self.pdf_vision_model = env.strip()
        if env := os.environ.get("PAPER_TOOLS_PDF_DPI"):
            self.pdf_dpi = int(env)
        if env := os.environ.get("PAPER_TOOLS_PDF_MAX_TOKENS"):
            self.pdf_max_output_tokens = int(env)
        if env := os.environ.get("PAPER_TOOLS_PDF_INPUT"):
            self.pdf_input = _clean_path_value(env)
        if env := os.environ.get("PAPER_TOOLS_PDF_EXTRACT_ONLY"):
            self.pdf_extract_only = env.strip().lower() in ("1", "true", "yes", "on")
        if env := os.environ.get("PAPER_TOOLS_OUTPUT"):
            self.output_dir = Path(_clean_path_value(env))
        if env := os.environ.get("PAPER_TOOLS_LOG_LEVEL"):
            self.log_level = env
        if env := os.environ.get("PAPER_TOOLS_CONCURRENCY"):
            self.translate_concurrency = int(env)
        if env := os.environ.get("PAPER_TOOLS_IMG_LOCAL"):
            self.image_local = env.strip().lower() in ("1", "true", "yes", "on")
        if env := os.environ.get("PAPER_TOOLS_DL_RETRIES"):
            self.download_max_retries = int(env)
        if env := os.environ.get("PAPER_TOOLS_PROXY"):
            self.download_proxy = env.strip()
        if env := os.environ.get("PAPER_TOOLS_CORS_PROXY"):
            self.cors_proxy = env.strip()
        if env := os.environ.get("PAPER_TOOLS_CITE_SEARCH"):
            self.cite_search_engine = env.strip().lower()
        if env := os.environ.get("PAPER_TOOLS_CITE_DISPLAY"):
            self.cite_display_mode = env.strip().lower()
        if env := os.environ.get("PAPER_TOOLS_NAME_MODE"):
            self.output_name_mode = env.strip().lower()
        if env := os.environ.get("PAPER_TOOLS_MERGE_MIN"):
            self.merge_min_chars = int(env)
        if env := os.environ.get("PAPER_TOOLS_MERGE_MAX"):
            self.merge_target_max = int(env)
        if env := os.environ.get("PAPER_TOOLS_TOKEN_REPORT"):
            self.token_report = env.strip().lower() in ("1", "true", "yes", "on")
        if env := os.environ.get("PAPER_TOOLS_PRICING_PARSER"):
            self.pricing_parser = env.strip().lower()
        if env := os.environ.get("PAPER_TOOLS_EXPORT_FORMATS"):
            self.export_formats = env.strip().lower()
        if env := os.environ.get("PAPER_TOOLS_OUTPUT_MD"):
            self.output_markdown = env.strip().lower() in ("1", "true", "yes", "on")
        if env := os.environ.get("PAPER_TOOLS_SKIP_TRANSLATE"):
            self.translate_skip = env.strip().lower() in ("1", "true", "yes", "on")
        if env := os.environ.get("PAPER_TOOLS_INPUT"):
            self.arxiv_input = _clean_path_value(env)
        if env := os.environ.get("PAPER_TOOLS_SUMMARY_MAX_CHARS"):
            self.summary_max_abstract_chars = int(env)
        if env := os.environ.get("PAPER_TOOLS_RESUME_MODE"):
            self.resume_mode = env.strip().lower()
        if env := os.environ.get("PAPER_TOOLS_OVERWRITE"):
            self.overwrite_mode = env.strip().lower()
        if env := os.environ.get("PAPER_TOOLS_MD_INPUT"):
            self.md_input = _clean_path_value(env)
        if env := os.environ.get("PAPER_TOOLS_MD_EXPORT_FORMATS"):
            self.md_export_formats = env.strip().lower()
        if env := os.environ.get("PAPER_TOOLS_MD_FONT"):
            self.md_export_font = env.strip()
        if env := os.environ.get("PAPER_TOOLS_MD_PORTABLE_KEEP_SVG"):
            self.md_portable_keep_svg = env.strip().lower() in ("1", "true", "yes", "on")
        if env := os.environ.get("PAPER_TOOLS_MD_MERMAID"):
            self.md_mermaid_renderer = env.strip().lower()
        if env := os.environ.get("PAPER_TOOLS_MD_MERMAID_ENDPOINT"):
            self.md_mermaid_endpoint = env.strip()
        if env := os.environ.get("PAPER_TOOLS_MD_MERMAID_WIDTH"):
            self.md_mermaid_width = int(env)
        if env := os.environ.get("PAPER_TOOLS_MD_MERMAID_THEME"):
            self.md_mermaid_theme = env.strip()
        if env := os.environ.get("PAPER_TOOLS_MD_MERMAID_MMDC"):
            self.md_mermaid_mmdc = _clean_path_value(env)
        return self


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """获取全局单例配置。"""
    return AppSettings().resolve()


def reset_settings() -> None:
    """清除缓存（主要用于测试）。"""
    get_settings.cache_clear()
