"""Markdown → Word / PDF 导出工具 —— 可直接用 IDE 运行的入口。

运行方式（任选其一）：
    1. 在 IDE 中右键本文件 -> Run / Debug（无需命令行参数）
    2. 命令行：python paper_tools/tools/md_export/main.py
    3. 包模式：python -m paper_tools.tools.md_export.main
    4. 根入口：python main.py md-export <markdown路径> [--format docx|pdf|all]

无需命令行参数：直接修改下方 `if __name__ == "__main__":` 里的常量即可。

与 pdf_translate/main.py 保持同样的结构：sys.path 引导 + 配置/日志初始化
+ IDE 常量区。本工具为离线转换（仅下载网络图片需联网），不依赖 API Key。
"""

import sys
from pathlib import Path

# 让脚本既能作为包内模块运行，也能直接作为脚本运行（IDE 右键 Run 即可）。
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from paper_tools.config import get_settings  # noqa: E402
from paper_tools.logging_setup import setup_logging  # noqa: E402
from paper_tools.tools.md_export import run  # noqa: E402


def main() -> None:
    # 应用配置 + 日志
    settings = get_settings()
    logger = setup_logging(settings.log_level)
    logger.info("Markdown 导出工具启动")

    # ===== 在这里填写参数 =====
    # 所有常量留空/保持默认时，会自动回退到 .env 对应的环境变量（见每条注释）。
    md_path = r""   # 待转换的 Markdown 文件路径，例如 r"D:/papers/paper.zh.md"
                    # 留空则用 .env 的 PAPER_TOOLS_MD_INPUT
    fmt = ""        # 输出格式：docx / pdf / docx_pdf / all
                    # 留空则用 .env 的 PAPER_TOOLS_MD_EXPORT_FORMATS（默认 docx_pdf）
    out_dir = ""    # 输出目录；留空则与源文件同目录
    overwrite = ""  # 输出文件覆盖策略：ask（询问，默认）/ always / never
                    # 留空则用 .env 的 PAPER_TOOLS_OVERWRITE 或默认 ask

    # ===== 参数生效 =====
    if out_dir:
        settings.output_dir = Path(out_dir)
    if overwrite:
        settings.overwrite_mode = overwrite

    # 待转换 Markdown：优先本文件的 md_path 常量，留空回退到 .env 的 PAPER_TOOLS_MD_INPUT。
    # 注意：刻意不回退到 PAPER_TOOLS_INPUT/PAPER_TOOLS_PDF_INPUT——分别是
    # arxiv 工具的链接/ID 与 pdf-translate 工具的 PDF 路径，复用会造成误用。
    input_arg = md_path if md_path else settings.md_input
    if not input_arg:
        logger.error("未指定 Markdown 文件：请在 main.py 的 md_path 常量填写路径，"
                     "或在 .env 设置 PAPER_TOOLS_MD_INPUT。")
        sys.exit(1)
    source = Path(str(input_arg)).expanduser()
    if not source.is_file() or source.suffix.lower() not in (".md", ".markdown"):
        logger.error(f"Markdown 文件不存在或不是 .md: {source}")
        sys.exit(1)

    outs = run(source,
               fmt=fmt or None,
               out_dir=out_dir or None,
               settings=settings)
    for f in outs:
        logger.info(f"结果文件: {f}")


if __name__ == "__main__":
    main()
