"""arxiv 论文翻译工具 —— 可直接用 IDE 运行的入口。

运行方式（任选其一）：
    1. 在 IDE 中右键本文件 -> Run / Debug（无需命令行参数）
    2. 命令行：python paper_tools/tools/arxiv_translate/main.py
    3. 包模式：python -m paper_tools.tools.arxiv_translate.main

无需命令行参数：直接修改下方 `if __name__ == "__main__":` 里的常量即可。
"""

import sys
from pathlib import Path

# 让脚本既能作为包内模块运行，也能直接作为脚本运行（IDE 右键 Run 即可）。
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from paper_tools.config import get_settings  # noqa: E402
from paper_tools.logging_setup import setup_logging  # noqa: E402
from paper_tools.tools.arxiv_translate.pipeline import run  # noqa: E402


def main() -> None:
    # 应用配置 + 日志
    settings = get_settings()
    logger = setup_logging(settings.log_level)
    logger.info("arxiv 论文翻译工具启动")

    # ===== 在这里填写参数 =====
    INPUT = "2605.26158v1"  # arxiv 链接或 ID，例如 "https://arxiv.org/abs/2605.26158v1" 或 "2605.26158v1"
    API_KEY = ""            # 留空则用 .env / 环境变量里的 DEEPSEEK_API_KEY
    MODEL = ""              # 留空则用配置里的默认模型（deepseek-v4-flash）
    OUT_DIR = ""            # 留空则用配置里的默认输出目录（项目根/output）
    # 引用搜索引擎：google | bing | duckduckgo | semantic_scholar | arxiv（默认 bing，国内可访问）
    CITE_SEARCH = "bing"
    # 引用显示模式：short = 只显示作者年份（论文名靠 hover 提示，短链不占行宽）
    #              title = 显示作者年份 + 完整论文名（信息全但占行宽）
    CITE_DISPLAY = "short"
    # 图片本地模式：True = 下载图片到本地并改用本地相对路径；False = 引用保持原网络 URL（不下载）
    IMAGE_LOCAL = False
    # 短块合并阈值（按纯文本字符数）：相邻同类过短文本块（段落/列表项）会合并到前一块
    # 一起翻译，减少碎片、保持上下文。设为 0 关闭合并。
    MERGE_MIN_CHARS = 50

    # ===== 参数生效 =====
    if API_KEY:
        settings.llm.api_key = API_KEY
    if MODEL:
        settings.llm.model = MODEL
    if OUT_DIR:
        settings.output_dir = Path(OUT_DIR)
    if CITE_SEARCH:
        settings.cite_search_engine = CITE_SEARCH.lower()
    if CITE_DISPLAY:
        settings.cite_display_mode = CITE_DISPLAY.lower()
    if str(IMAGE_LOCAL).strip().lower() in ("1", "true", "yes", "on"):
        settings.image_local = True
    if MERGE_MIN_CHARS:
        settings.merge_min_chars = int(MERGE_MIN_CHARS)

    if not settings.llm.api_key:
        logger.error("未配置 API key：请在 .env 设置 DEEPSEEK_API_KEY，或在下方 API_KEY 常量中填写。")
        sys.exit(1)

    out = run(INPUT)
    logger.info(f"结果文件: {out}")


if __name__ == "__main__":
    main()