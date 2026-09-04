# paper-tools

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

学术论文相关实用工具集合。基于大语言模型（LLM），提供论文翻译、解析、下载等一站式工具链。

## 已支持工具

| 工具 | 说明 |
|------|------|
| [arxiv-translate](./paper_tools/tools/arxiv_translate/README.md) | 下载 arxiv HTML 论文，解析公式/图表/引用，调用 DeepSeek 翻译为中文 Markdown |
| [pdf-translate](./paper_tools/tools/pdf_translate/README.md) | 本地 PDF 逐页视觉提取、跨页续接，再翻译为中文 Markdown |

> 更多工具正在规划中，欢迎 [贡献](#贡献) 或提交 issue。

## 特性

- **公式完整保护**：LaTeX 公式以占位符保护，翻译后精确还原，不会被篡改或丢失
- **术语一致性**：自动构建术语表，锁定全篇专有名词与自创方法名译法
- **结构保留**：章节、段落、列表、图片、表格、文本框/提示框均按原结构落地
- **引用可跳转**：论文引用自动生成搜索引擎链接（Google / Bing / DuckDuckGo / Semantic Scholar / arXiv）
- **翻译质量保障**：论文立场锚定 + 翻译后一致性检查与自动返修
- **高并发**：多线程并发翻译，默认 8 线程
- **原文模式**：可跳过 LLM，仅解析并导出结构化英文原文（无需 API Key）
- **断点续译**：异常退出后可自动/手动恢复，避免从头重翻
- **受限网络可用**：支持标准 CONNECT 代理与轻量 CORS 文本转发代理
- **统一配置**：所有工具共享配置体系，通过 `.env` 或环境变量管理

## 工作原理

一句话概括：**下载 → 解析 → 术语/立场准备 → 并发翻译 → 一致性返修 → 输出**。

各步骤的实现细节（公式占位符保护、术语表与缩写预热、论文立场摘要、短块合并、一致性检查与返修、Token 用量统计等）见
[arxiv-translate 文档 · 翻译策略详解](./paper_tools/tools/arxiv_translate/README.md#翻译策略详解)。

## 快速开始

### 1. 环境要求

- Python 3.12+
- 推荐使用 [uv](https://github.com/astral-sh/uv) 管理依赖

### 2. 安装

```bash
# 克隆仓库
git clone https://github.com/The-cute-cat/paper-tools.git
cd paper-tools

# 创建虚拟环境并安装
uv venv
uv pip install -e .

# 配置 API Key
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY
```

### 3. 运行

```bash
# 翻译一篇 arxiv 论文
python main.py arxiv-translate https://arxiv.org/abs/2605.26158

# 或直接使用 arxiv ID
python main.py arxiv-translate 2605.26158v1

# 指定输出目录 / 模型 / API Key（覆盖配置）
python main.py arxiv-translate 2605.26158v1 --out ./my-output --model deepseek-chat

# 额外导出 docx / pdf（实验中 experimental）；--no-md 可只产出导出格式
python main.py arxiv-translate 2605.26158v1 --export docx,pdf --no-md

# 翻译本地 PDF（逐页视觉识别 → 翻译）
python main.py pdf-translate "D:/papers/paper.pdf"
python main.py pdf-translate "D:/papers/paper.pdf" --dpi 180 --extract-only
```

各工具的输出文件、目录结构与进阶用法见对应工具文档：
[arxiv-translate](./paper_tools/tools/arxiv_translate/README.md#输出说明) ·
[pdf-translate](./paper_tools/tools/pdf_translate/README.md#输出)。

## 配置

所有配置通过 `.env` 文件（项目根目录）或环境变量设置，无需修改代码。

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DEEPSEEK_API_KEY` | DeepSeek API Key（必填） | — |
| `DEEPSEEK_MODEL` | 模型名称 | `deepseek-v4-flash` |
| `DEEPSEEK_BASE_URL` | API 地址 | `https://api.deepseek.com` |
| `PAPER_TOOLS_OUTPUT` | 输出根目录 | `./output` |
| `PAPER_TOOLS_LOG_LEVEL` | 日志级别 | `INFO` |
| `PAPER_TOOLS_CONCURRENCY` | 并发翻译线程数（0/1=单线程） | `8` |
| `PAPER_TOOLS_IMG_LOCAL` | 是否下载图片到本地 | `false` |
| `PAPER_TOOLS_DL_RETRIES` | 图片等二进制下载失败时的重试次数 | `3` |
| `PAPER_TOOLS_PROXY` | 下载代理（标准 CONNECT 隧道，支持图片等二进制下载），如 `http://127.0.0.1:7890`；留空直连 | 空 |
| `PAPER_TOOLS_CORS_PROXY` | 轻量 CORS 文本转发代理（仅文本/HTML 下载，不支持二进制），如 `https://worker.dev/?url=` | 空 |
| `PAPER_TOOLS_MERGE_MIN` | 翻译单元目标长度下限（字符，0=关闭合并） | `1000` |
| `PAPER_TOOLS_MERGE_MAX` | 翻译单元目标长度上限（字符，0=不限制） | `1500` |
| `PAPER_TOOLS_CITE_SEARCH` | 引用搜索引擎（google/bing/duckduckgo/semantic_scholar/arxiv） | `bing` |
| `PAPER_TOOLS_CITE_DISPLAY` | 引用显示模式（short/title） | `short` |
| `PAPER_TOOLS_NAME_MODE` | 输出文件命名方式（id/title/title_zh） | `id` |
| `PAPER_TOOLS_TOKEN_REPORT` | 翻译结束后在日志输出 token 用量与费用估算（1/true 开启） | `false` |
| `PAPER_TOOLS_EXPORT_FORMATS` | 额外导出格式（docx/pdf/docx_pdf/all，逗号分隔；实验中 experimental） | 空 |
| `PAPER_TOOLS_OUTPUT_MD` | 导出额外格式时是否仍输出 `.zh.md`（1/true 默认） | `true` |
| `PAPER_TOOLS_SKIP_TRANSLATE` | 跳过翻译，仅解析并输出论文英文原文（1/true；开启后无需 API Key） | `false` |
| `PAPER_TOOLS_INPUT` | 待翻译的 arxiv 链接或 ID（命令行/INPUT 常量未提供时的回退） | 空 |
| `PAPER_TOOLS_SUMMARY_MAX_CHARS` | 立场摘要截断上限（字符，0=不截断） | `0` |
| `PAPER_TOOLS_RESUME_MODE` | 断点续译模式：`ask`(终端询问) / `auto`(自动恢复) / `never`(从头重翻) | `ask` |

> **价目表缓存**：Token 费用估算依赖 DeepSeek 官方定价。价目表不会写死在代码中，而是在首次使用时从官方定价页实时抓取，并缓存到 `paper_tools/core/pricing_cache.json`（默认 24 小时有效）。抓取失败时自动回退到最近一次成功缓存；若缓存与实时抓取均失败则报错提示。如需强制刷新价目表，删除该缓存文件后重新运行即可。

## 项目结构

```
paper-tools/
├── main.py                              # 统一 CLI 入口（子命令分发）
├── pyproject.toml                       # 项目元数据与依赖
├── .env.example                         # 配置模板
├── paper_tools/
│   ├── config.py                        # 统一配置管理（.env + 环境变量）
│   ├── logging_setup.py                 # 统一日志
│   ├── core/                            # 可复用的核心模块
│   │   ├── translator.py                #   LLM 翻译器（公式保护、术语约束、JSON 输出、token 统计）
│   │   ├── translator_prompts.yaml      #   翻译/检查 prompt 模板
│   │   ├── downloader.py                #   通用下载（文本/二进制，带浏览器伪装头与代理）
│   │   ├── exporter.py                  #   DOCX / PDF 导出（实验性）
│   │   ├── math_render.py               #   公式渲染辅助
│   │   ├── glossary.py                  #   术语表（翻译记忆）
│   │   └── pricing.py                   #   DeepSeek 价目表动态获取与本地缓存
│   └── tools/                           # 工具目录（每个子目录是一个独立工具）
│       ├── arxiv_translate/             #   工具：arxiv 论文翻译
│       │   ├── README.md                #     工具详细文档
│       │   ├── main.py                  #     独立入口（可直接 IDE 运行）
│       │   ├── pipeline.py              #     翻译流水线（下载→解析→翻译→写出）
│       │   └── parser.py                #     ar5iv HTML 解析器
│       └── pdf_translate/               #   工具：本地 PDF 论文翻译
│           ├── README.md                #     工具详细文档
│           ├── main.py                  #     独立入口（可直接 IDE 运行）
│           ├── pipeline.py              #     提取 + 翻译流水线
│           └── extractor.py             #     逐页渲染与视觉提取
└── output/                              # 默认输出目录（gitignore）
```

## 添加新工具

在 `paper_tools/tools/<your_tool>/` 下创建子包，实现 `run(...)` 函数，然后在 `main.py` 的 `build_parser()` 中注册子命令即可。核心模块（`translator`、`downloader`、`glossary`）均可直接复用。

示例：

```python
# paper_tools/tools/my_tool/__init__.py
from .pipeline import run
__all__ = ["run"]
```

```python
# main.py 中注册
p = sub.add_parser("my-tool", help="我的工具")
p.add_argument("input")
```

## 贡献

欢迎提交 Issue 和 Pull Request。新增工具请遵循现有结构，并附带对应 `README.md` 文档。

## License

MIT
