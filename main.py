"""paper-tools 统一命令行入口。

用法：
    python main.py arxiv-translate <arxiv链接或ID> [--out 目录] [--model xxx]
    python main.py arxiv-translate 2605.26158v1

可用工具：
    arxiv-translate   下载 arxiv HTML 论文并翻译为中文 markdown
    pdf-translate     本地 PDF 逐页视觉提取并翻译为中文 markdown

环境变量（在 .env 或系统中配置，详见 README）：
    DEEPSEEK_API_KEY      必填，DeepSeek API key
    DEEPSEEK_MODEL        可选，模型名（默认 deepseek-v4-flash）
    DEEPSEEK_BASE_URL     可选，API 地址
    PAPER_TOOLS_OUTPUT    可选，默认输出根目录
    PAPER_TOOLS_LOG_LEVEL 可选，日志级别（默认 INFO）
    PAPER_TOOLS_CONCURRENCY  可选，并发翻译线程数（默认 8）
    PAPER_TOOLS_MERGE_MIN / _MAX  可选，翻译单元长度下限/上限（默认 1000/1500）
    PAPER_TOOLS_CITE_SEARCH / _DISPLAY  可选，引用搜索引擎/显示模式
    PAPER_TOOLS_NAME_MODE  可选，输出文件命名方式（id/title/title_zh）
    PAPER_TOOLS_TOKEN_REPORT  可选，翻译后输出 token 用量与费用估算
    PAPER_TOOLS_EXPORT_FORMATS / _OUTPUT_MD  可选，额外导出格式相关
"""

import argparse
import sys

from paper_tools.config import get_settings
from paper_tools.logging_setup import setup_logging
from paper_tools.tools import arxiv_translate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paper-tools", description="论文相关实用工具集合")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("arxiv-translate", help="下载 arxiv HTML 论文并翻译为中文 markdown")
    p.add_argument("input", help="arxiv 链接 (https://arxiv.org/abs/...) 或 ID (如 2605.26158v1)")
    p.add_argument("--out", default=None, help="输出根目录（默认读配置 PAPER_TOOLS_OUTPUT）")
    p.add_argument("--model", default=None, help="DeepSeek 模型名（覆盖配置）")
    p.add_argument("--api-key", default=None, help="DeepSeek API key（覆盖环境变量）")
    p.add_argument("--export", default=None,
                   help="额外导出格式：docx、pdf、docx_pdf、all（逗号分隔，如 --export docx,pdf）")
    p.add_argument("--no-md", action="store_true",
                   help="不输出 .zh.md，仅导出 docx/pdf（默认两种都输出）")
    p = sub.add_parser("pdf-translate", help="逐页识别本地 PDF 论文并翻译为中文 Markdown")
    p.add_argument("input", help="本地 PDF 文件路径")
    p.add_argument("--out", default=None, help="输出根目录")
    p.add_argument("--model", default=None, help="翻译模型")
    p.add_argument("--vision-model", default=None, help="识图模型")
    p.add_argument("--api-key", default=None, help="DeepSeek API key")
    p.add_argument("--dpi", type=int, default=None, help="页面渲染 DPI（72-300）")
    p.add_argument("--max-output-tokens", type=int, default=None,
                   help="视觉模型单页识别最大输出 token（密集页截断时调大）")
    p.add_argument("--extract-only", action="store_true", help="仅执行识图提取（仍调用 API）")
    p.add_argument("--no-resume", action="store_true", help="不复用逐页识别缓存")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    # 应用配置 + 日志
    settings = get_settings()
    if args.command == "arxiv-translate":
        if args.out:
            settings.output_dir = __import__("pathlib").Path(args.out)
        if args.model:
            settings.llm.model = args.model
        if args.api_key:
            settings.llm.api_key = args.api_key
        if args.export:
            settings.export_formats = args.export.strip().lower()
        if args.no_md:
            settings.output_markdown = False

    logger = setup_logging(settings.log_level)

    try:
        if args.command == "pdf-translate":
            from pathlib import Path
            from paper_tools.tools.pdf_translate import run

            if args.out:
                settings.output_dir = Path(args.out)
            if args.model:
                settings.llm.model = args.model
            if args.api_key:
                settings.llm.api_key = args.api_key
            if args.vision_model:
                settings.pdf_vision_model = args.vision_model
            if args.dpi is not None:
                settings.pdf_dpi = args.dpi
            if args.max_output_tokens is not None:
                settings.pdf_max_output_tokens = args.max_output_tokens
            out = run(args.input, settings=settings,
                      extract_only=args.extract_only or settings.translate_skip,
                      resume=not args.no_resume)
            logger.info(f"结果文件: {out}")
        elif args.command == "arxiv-translate":
            out = arxiv_translate.run(args.input)
            logger.info(f"结果文件: {out}")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"执行失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
