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


@dataclass
class LLMSettings:
    """大模型（翻译）相关配置。"""
    provider: str = "deepseek"
    api_key: str = ""
    model: str = "deepseek-v4-flash"
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
    pdf_vision_model: str = "deepseek-v4-flash-vision-exp"
    pdf_dpi: int = 160
    # 视觉模型单页识别的最大输出 token 数。
    # 内容密集的页面（多公式/长表格）可能超出默认上限导致输出被截断，
    # 此时响应 finish_reason=length，校验失败并重试——但重试不会改变上限，
    # 必然再次失败。遇到这种情况请调大本值（受模型上下文上限约束）；
    # 降低 DPI 无法解决，因为它不减少输出 token。
    pdf_max_output_tokens: int = 16384

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
        if env := os.environ.get("PAPER_TOOLS_OUTPUT"):
            self.output_dir = Path(env)
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
        if env := os.environ.get("PAPER_TOOLS_EXPORT_FORMATS"):
            self.export_formats = env.strip().lower()
        if env := os.environ.get("PAPER_TOOLS_OUTPUT_MD"):
            self.output_markdown = env.strip().lower() in ("1", "true", "yes", "on")
        if env := os.environ.get("PAPER_TOOLS_SKIP_TRANSLATE"):
            self.translate_skip = env.strip().lower() in ("1", "true", "yes", "on")
        if env := os.environ.get("PAPER_TOOLS_INPUT"):
            self.arxiv_input = env.strip()
        if env := os.environ.get("PAPER_TOOLS_SUMMARY_MAX_CHARS"):
            self.summary_max_abstract_chars = int(env)
        if env := os.environ.get("PAPER_TOOLS_RESUME_MODE"):
            self.resume_mode = env.strip().lower()
        return self


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """获取全局单例配置。"""
    return AppSettings().resolve()


def reset_settings() -> None:
    """清除缓存（主要用于测试）。"""
    get_settings.cache_clear()
