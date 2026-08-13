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
    download_headers: dict = field(default_factory=lambda: {"User-Agent": "paper-tools/1.0"})
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

    def resolve(self) -> "AppSettings":
        """用环境变量/默认值补全缺失字段。"""
        _load_env()
        if not self.llm.api_key:
            self.llm.api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if env := os.environ.get("DEEPSEEK_MODEL"):
            self.llm.model = env
        if env := os.environ.get("DEEPSEEK_BASE_URL"):
            self.llm.base_url = env
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
        return self


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """获取全局单例配置。"""
    return AppSettings().resolve()


def reset_settings() -> None:
    """清除缓存（主要用于测试）。"""
    get_settings.cache_clear()
