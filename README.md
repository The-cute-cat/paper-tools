# paper-tools

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

学术论文相关实用工具集合。基于大语言模型（LLM），提供论文翻译、解析、下载等一站式工具链。

## 已支持工具

| 工具 | 说明 |
|------|------|
| [arxiv-translate](./paper_tools/tools/arxiv_translate/README.md) | 下载 arxiv HTML 论文，解析公式/图表/引用，调用 DeepSeek 翻译为中文 Markdown |

> 更多工具正在规划中，欢迎 [贡献](#贡献) 或提交 issue。

## 特性

- **公式完整保护**：LaTeX 公式自动识别并用占位符保护，翻译后精确还原，不会出现公式被篡改或丢失
- **术语一致性**：自动构建术语表（Glossary），锁定全篇专有名词和自创方法名译法，消除前后不一致
- **引用智能处理**：自动识别论文引用，生成可跳转的搜索引擎链接（支持 Google / Bing / DuckDuckGo / Semantic Scholar / arXiv）
- **图表保留**：图片和表格结构完整保留，表格内容按单元格翻译
- **并发翻译**：支持多线程并发翻译，大幅提升速度
- **一致性检查与返修**：翻译完成后自动检查译文质量，对违和词、术语不一致等自动返修
- **中英排版优化**：自动在中英文、数字之间插入空格（pangu 风格），提升阅读体验
- **统一配置**：所有工具共享配置体系，通过 `.env` 文件或环境变量管理

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
```

运行后将在 `output/<arxiv_id>/` 下生成：
- `<arxiv_id>.zh.md` — 中文翻译 Markdown
- `<arxiv_id>.glossary.json` — 术语表（JSON 格式，可人工校对复用）
- `<arxiv_id>.html` — 原始 HTML 备份

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
| `PAPER_TOOLS_DL_RETRIES` | 图片下载重试次数 | `3` |
| `PAPER_TOOLS_MERGE_MIN` | 短文本块合并阈值（字符，0=关闭） | `50` |
| `PAPER_TOOLS_CITE_SEARCH` | 引用搜索引擎（google/bing/duckduckgo/semantic_scholar/arxiv） | `bing` |
| `PAPER_TOOLS_CITE_DISPLAY` | 引用显示模式（short/title） | `short` |
| `PAPER_TOOLS_EXPORT_FORMATS` | 额外导出格式（**开发中，暂未启用**，预留 docx/pdf/all） | 空 |

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
│   │   ├── translator.py                #   LLM 翻译器（公式保护、术语约束、JSON 输出）
│   │   ├── downloader.py                #   通用下载（文本/二进制，带重试）
│   │   └── glossary.py                  #   术语表（翻译记忆）
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
