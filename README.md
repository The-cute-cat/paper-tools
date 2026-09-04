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

- **公式完整保护**：LaTeX 公式自动识别并用占位符保护，翻译后精确还原，不会被篡改或丢失；同时公式原文作为「公式表」上下文随翻译单元一并发给 LLM，帮助模型理解公式语义、提升译文准确性（模型只回抄占位符，不回写 LaTeX）
- **术语一致性**：自动构建术语表（Glossary），锁定全篇专有名词和自创方法名译法，消除前后不一致
- **引用智能处理**：自动识别论文引用，生成可跳转的搜索引擎链接（支持 Google / Bing / DuckDuckGo / Semantic Scholar / arXiv）
- **图表保留**：图片和表格结构完整保留，表格内容按单元格翻译
- **文本框/提示框保留**：定理、备注、Prompt 示例等带边框文本框以引用块（`> `）形式保留，强调标记不丢失
- **原文模式**：可跳过 LLM，仅解析并导出结构化英文原文（无需 API Key）
- **断点续译**：异常退出后检测到翻译缓存可自动/手动恢复，避免从头重翻
- **网络代理**：支持标准 CONNECT 代理与轻量 CORS 文本转发代理，适配受限网络访问 arxiv
- **并发翻译**：支持多线程并发翻译，大幅提升速度
- **一致性检查与返修**：翻译完成后自动检查译文质量，对违和词、术语不一致等自动返修
- **中英排版优化**：自动在中英文、数字之间插入空格（pangu 风格），提升阅读体验
- **Token 用量与费用估算**：开启后在日志输出总 token 消耗、缓存命中/未命中占比，并依据官方价目表估算费用（价目表运行时动态获取并本地缓存，不写死在代码中）
- **统一配置**：所有工具共享配置体系，通过 `.env` 文件或环境变量管理

## 翻译工作流程

以 `arxiv-translate` 为例，单篇论文的处理流程如下：

```
下载 arxiv HTML（ar5iv）
    │
    ▼
解析（parser.py）：抽取标题/摘要/章节/段落/列表/公式/图表/表格/引用
    │
    ▼
构建初始术语表（单线程，基于标题+前几段）
    │
    ▼
生成论文立场摘要（锚定翻译基调，零成本拼接，无额外 LLM 调用）
    │
    ▼
缩写定义预热（扫描 ABBR=英文 入库，优先翻译图注/定义句以锁定缩写译法）
    │
    ▼
并发翻译各内容块（按翻译单元调度，多线程）
  ├─ 公式 → 占位符保护（原文作上下文发给 LLM 助其理解语义，仅回抄占位符）
  ├─ 文本 → 术语约束 + 立场锚定
  ├─ 表格 → 异步批量翻译
  └─ 短块合并 → JSON 分块翻译（合并组各子块独立回收）
    │
    ▼
一致性检查与返修（按翻译单元判定，合并组任一子块不通过则整组重翻）
    │
    ▼
写出 Markdown / 额外格式（docx/pdf）
```

## 核心机制

### 术语表与缩写预热

术语表（Glossary）由论文自身文本驱动，分三阶段构建：

1. **建表阶段**：注入领域默认术语种子后，取"标题 + 摘要及前几段（至多 4 段）"发一次翻译，让模型补充自创词/易错词译法；自创方法/框架名标记为 `<KEEP>`（保留英文，如 `Furina`）。
2. **缩写定义预热**：扫描英文原文中的 `ABBR = English` 缩写定义入库；再单线程优先翻译含缩写定义的块——图注/表注/伪代码注及出现 `ABBR = English` 的段落——将其译法 `ingest` 进术语表。这些块在并发阶段直接复用、不再重翻，从而保证表格表头与图注里的同一缩写译法一致。
3. **并发合并阶段**：翻译每块后，模型返回本块确定的新术语自动合并进共享术语表；缩写预热锁定的译法全程优先。

最终术语表保存为 JSON，可人工校对复用。

### 论文立场摘要

为抑制模型幻觉、保证全文立场一致，向 prompt 注入一句论文全局立场摘要。该摘要为**零成本**实现：直接用"标题 + 摘要前几段（至多 3 段，超 600 字截断）"拼接，要求模型据此判断核心任务/方法/立场并保持；缺失时回退到标题，再缺失时仅提示"保持立场一致"。摘要仅作为内部 prompt 上下文，不向用户控制台输出全文。

### 短块合并

相邻同类文本块（段落 / 列表项 / 章节标题）贪心合并为"翻译单元"，目标落入 `[PAPER_TOOLS_MERGE_MIN, PAPER_TOOLS_MERGE_MAX]`（默认 `1000` / `1500`，`MERGE_MIN=0` 关闭）字符区间：

- 仅同类型块可并入同一单元，标题/公式/图表/表格等结构块作为天然分隔符不被合并；
- 单块已达上限不强行拆分，独立成单元；
- 合并组以 JSON 数组分块发送给模型，各块译文独立回收（互不拼接）；
- 翻译与返修按翻译单元调度，合并组任一子块需返修则整组重翻。

### 一致性检查与返修

翻译完成后逐块检查译文质量（违和词、术语不一致等），不通过则把整组合并组重新发送给模型翻译，直到整组通过或达到最大尝试次数（由 `PAPER_TOOLS_TRANSLATE_REPAIR` 开关控制）。

### Token 用量与费用估算

翻译器在 `TokenUsage` 中线程安全地累计每次调用的 token（输入/输出/缓存命中/未命中/请求数）。开启 `PAPER_TOOLS_TOKEN_REPORT` 后，翻译结束输出总消耗、缓存命中占比，并依据官方价目表估算费用。价目表由 `core/pricing.py` 运行时从 DeepSeek 官方定价页动态抓取并本地缓存（`pricing_cache.json`，默认 24h 有效），不写死在代码中。

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

# 指定输出目录
python main.py arxiv-translate 2605.26158v1 --out ./my-output

# 指定模型 / API Key（覆盖配置）
python main.py arxiv-translate 2605.26158v1 --model deepseek-chat --api-key sk-xxx

# 额外导出 docx / pdf（实验中 experimental）；--no-md 可只产出导出格式
python main.py arxiv-translate 2605.26158v1 --export docx,pdf --no-md
```

运行后将在 `output/<arxiv_id>/` 下生成：
- `<arxiv_id>.zh.md` — 中文翻译 Markdown
- `<arxiv_id>.glossary.json` — 术语表（JSON 格式，可人工校对复用）
- `<arxiv_id>.html` — 原始 HTML 备份
- `<arxiv_id>.zh.html` / `<arxiv_id>.zh.docx` / `<arxiv_id>.zh.pdf` — 中文 HTML 及额外导出格式（受 `PAPER_TOOLS_EXPORT_FORMATS` 控制，实验中 experimental）
- `images/` — 图片目录（仅当 `PAPER_TOOLS_IMG_LOCAL=true`）

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
│       └── arxiv_translate/             #   工具：arxiv 论文翻译
│           ├── README.md                #     工具详细文档
│           ├── __init__.py
│           ├── main.py                  #     独立入口（可直接 IDE 运行）
│           ├── pipeline.py              #     翻译流水线（下载→解析→翻译→写出）
│           └── parser.py                #     ar5iv HTML 解析器
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
## 本地 PDF 论文翻译

新增 `pdf-translate`：先逐页识图生成原文 Markdown，再复用现有翻译器生成中文 Markdown。

```bash
uv sync
uv run python main.py pdf-translate "D:/papers/paper.pdf"
uv run python main.py pdf-translate "D:/papers/paper.pdf" --extract-only
```

需要配置 `DEEPSEEK_API_KEY`，**仅提取也会调用收费视觉 API**，页面及插图将发送至配置的 DeepSeek 服务。
支持 `--out`、`--model`、`--vision-model`、`--dpi` 和 `--no-resume`。
详见 [PDF 工具说明](paper_tools/tools/pdf_translate/README.md)。
