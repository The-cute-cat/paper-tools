"""本地 PDF 论文翻译工具 —— 可直接用 IDE 运行的入口。

运行方式（任选其一）：
    1. 在 IDE 中右键本文件 -> Run / Debug（无需命令行参数）
    2. 命令行：python paper_tools/tools/pdf_translate/main.py
    3. 包模式：python -m paper_tools.tools.pdf_translate.main

无需命令行参数：直接修改下方 `if __name__ == "__main__":` 里的常量即可。

与 arxiv_translate/main.py 保持同样的结构：sys.path 引导 + 配置/日志初始化
+ IDE 常量区，不依赖根目录 main.py（避免从任意工作目录运行时找不到入口）。
"""

import sys
from pathlib import Path

# 让脚本既能作为包内模块运行，也能直接作为脚本运行（IDE 右键 Run 即可）。
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from paper_tools.config import get_settings  # noqa: E402
from paper_tools.logging_setup import setup_logging  # noqa: E402
from paper_tools.tools.pdf_translate.pipeline import run  # noqa: E402


def main() -> None:
    # 应用配置 + 日志
    settings = get_settings()
    logger = setup_logging(settings.log_level)
    logger.info("PDF 论文翻译工具启动")

    # ===== 在这里填写参数 =====
    pdf_path = ""       # 本地 PDF 文件路径，例如 r"D:/papers/paper.pdf"（不支持下载 URL）
    api_key = ""        # 留空则用 .env / 环境变量里的 DEEPSEEK_API_KEY
    model = ""          # 第二阶段翻译模型，留空则用配置默认（deepseek-v4-flash）
    vision_model = ""   # 第一阶段视觉提取模型，留空则用配置默认（deepseek-v4-flash-vision-exp）
    out_dir = ""        # 留空则用配置里的默认输出目录（项目根/output）
    dpi = 0             # 页面渲染 DPI（72-300），0 表示用配置默认（160）
    max_output_tokens = 0  # 视觉模型单页识别最大输出 token；0 表示用配置默认（16384）。
                           # 密集页输出被截断时调大它（降低 DPI 无效，它不减少输出长度）。
    # 仅提取：True 时只做第一阶段逐页识别，输出 .extracted.md 后结束。
    # 注意：这不是离线模式，第一阶段仍需调用视觉模型并产生费用。
    extract_only = False
    # 断点续识别：True 时复用有效的逐页缓存（默认开启）；False 强制重新识别。
    resume = True
    # 跳过第二阶段翻译（等价于 --extract-only）：True 时只产出原文。
    # 环境变量 PAPER_TOOLS_SKIP_TRANSLATE 已生效时无需再设。
    translate_skip = False
    # Token 用量报告：True 时翻译结束后输出 token 消耗与费用估算。
    token_report = False

    # ===== 参数生效 =====
    if api_key:
        settings.llm.api_key = api_key
    if model:
        settings.llm.model = model
    if vision_model:
        settings.pdf_vision_model = vision_model
    if out_dir:
        settings.output_dir = Path(out_dir)
    if dpi:
        settings.pdf_dpi = int(dpi)
    if max_output_tokens:
        settings.pdf_max_output_tokens = int(max_output_tokens)
    if str(token_report).strip().lower() in ("1", "true", "yes", "on"):
        settings.token_report = True
    if str(translate_skip).strip().lower() in ("1", "true", "yes", "on"):
        settings.translate_skip = True

    # 待翻译 PDF：只认本文件的 pdf_path 常量。
    # 注意：这里刻意不回退到 .env 的 PAPER_TOOLS_INPUT——该变量是给 arxiv 工具用的
    # 链接/ID，若被本工具复用会出现"把 arxiv ID 当 PDF 路径"的误用。
    if not pdf_path:
        logger.error("未指定待翻译 PDF：请在 main.py 的 pdf_path 常量填写本地 PDF 路径"
                     "（本工具不支持下载 URL，也不复用 .env 的 PAPER_TOOLS_INPUT）。")
        sys.exit(1)
    source = Path(str(pdf_path)).expanduser()
    if not source.is_file() or source.suffix.lower() != ".pdf":
        logger.error(f"PDF 文件不存在或不是 .pdf（本工具仅支持本地、非空、未加密 PDF）: {source}")
        sys.exit(1)

    # 提取阶段同样调用视觉模型，因此无论是否跳过翻译都需要 API key。
    if not settings.llm.api_key:
        logger.error("未配置 API key：请在 .env 设置 DEEPSEEK_API_KEY，或在下方 api_key 常量中填写。")
        sys.exit(1)

    out = run(source, settings=settings,
              extract_only=extract_only or settings.translate_skip,
              resume=resume)
    logger.info(f"结果文件: {out}")


if __name__ == "__main__":
    main()
