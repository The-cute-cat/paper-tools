# arxiv-translate

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

将 arxiv 论文的 HTML 预览版（ar5iv 格式）下载并翻译为中文 Markdown，保留公式、图表、表格、引用等所有结构。

## 功能概览

- 输入 arxiv 链接或 ID，自动下载 HTML 预览版
- 解析论文结构：标题、章节、段落、公式、图表、列表、表格
- 公式完整保护（LaTeX 占位符机制），翻译后精确还原
- 自动构建术语表，全文术语锁定，消除前后译法不一致
- 引用智能处理：论文引用转为搜索引擎链接，方便查阅
- 图表结构保留：表格按单元格翻译，图片可选本地下载
- 多线程并发翻译，默认 8 线程
- 翻译后一致性检查与自动返修
- 中英文排版自动修复（pangu spacing）

## 使用方法

### CLI 方式

```bash
# 完整 arxiv 链接
python main.py arxiv-translate https://arxiv.org/abs/2605.26158

# 仅 arxiv ID
python main.py arxiv-translate 2605.26158v1

# 指定输出目录和模型
python main.py arxiv-translate 2605.26158v1 --out ./my-papers --model deepseek-chat
```

### IDE 直接运行

在 IDE 中打开 `paper_tools/tools/arxiv_translate/main.py`，修改 `main()` 函数中的常量即可直接 Run（无需配置命令行参数）：

```python
INPUT = "2605.26158v1"   # arxiv 链接或 ID
API_KEY = ""              # 留空则从 .env 读取
MODEL = ""                # 留空则用默认模型
OUT_DIR = ""              # 留空则用默认输出目录
CITE_SEARCH = "bing"      # 引用搜索引擎
CITE_DISPLAY = "short"    # 引用显示模式
IMAGE_LOCAL = False       # 是否下载图片到本地
MERGE_MIN_CHARS = 50      # 短块合并阈值
```

## 工作流程

```
用户输入（链接/ID）
    │
    ▼
解析 arxiv ID
    │
    ▼
下载 HTML 预览版 ──→ 保存原始 HTML
    │
    ▼
（可选）下载图片到本地
    │
    ▼
解析 HTML 结构
  ├─ 标题 / 章节标题
  ├─ 段落 / 列表项
  ├─ 行内/行间公式（LaTeX）
  ├─ 图片 / 表格（含 caption）
  └─ 参考文献引用
    │
    ▼
构建初始术语表（单线程，基于标题+摘要）
    │
    ▼
生成论文立场摘要（锚定翻译基调）
    │
    ▼
并发翻译各内容块（多线程）
  ├─ 公式 → 占位符保护
  ├─ 文本 → 术语约束 + 立场锚定
  ├─ 表格 → 异步批量翻译
  └─ 短块合并 → JSON 分块翻译
    │
    ▼
一致性检查与返修
  ├─ 占位符残留检测
  ├─ 学术违和词检测
  ├─ 术语沿用校验
  └─ 中文误译检测
    │
    ▼
组装输出 + 中英排版修复
    │
    ▼
输出 <id>.zh.md + <id>.glossary.json
```

## 翻译策略详解

### 公式保护

所有 LaTeX 公式（行内 `$...$` 和行间 `$$...$$`）在送入 LLM 前被替换为 `⟦MATH_n⟧` 占位符，翻译完成后再精确还原。同时兜底保护裸 LaTeX 命令（如 `\theta`, `\mathcal{Y}`）和指标变化表达式（如 `ASR↑`, `HDmax↓`），防止模型改写或注入乱码。

### 术语表（Glossary）

翻译前先基于论文标题和摘要构建初始术语表，锁定自创方法/框架名（如 `Furina` → 保留原文）和上下文敏感术语（如 `agent` → 智能体，而非"代理"）。

每条术语记录：
- **英文 key**：小写归一化的英文原文
- **中文译法**：锁定译法或 `KEEP` 标记（保留英文）
- **英文全称**（可选）：缩写对应的完整形式
- **备注**（可选）：如"论文自创框架名，勿译"

翻译每块后，模型返回本块确定的新术语，自动合并进共享术语表。最终术语表保存为 JSON，可人工校对复用。

### 引用处理

识别 HTML 中的 `ltx_cite` 引用节点，将论文名生成搜索引擎查询链接。支持两种显示模式：

- **short**（默认）：仅显示 `作者, 年份`，论文名作为链接 hover 提示（浏览器原生 title 属性）
- **title**：显示 `作者, 年份, 论文名`，信息完整但行宽较大

搜索引擎可通过 `PAPER_TOOLS_CITE_SEARCH` 或 `CITE_SEARCH` 常量切换（默认 Bing）。

### 一致性检查与返修

翻译完成后对每块译文进行质量检查：

1. **占位符残留**：检测 `⟦MATH_n⟧`、`<math` 等未还原的占位符
2. **学术违和词**：黑名单匹配常见机器翻译错误（如"锐利边界"→ 应译"清晰边界"）
3. **术语非沿用**：检测应保留英文的词是否被翻译、应译特定中文的词是否用了错误近义词
4. **中文误译**：检测 `WRONG_VARIANT_MAP` 中的已知错误表达

检测不通过则返修——将整组块重新发送给模型翻译，直到通过检查或达到最大尝试次数。

### 短块合并

相邻的同类型短文本（段落/列表项，默认阈值 50 字符）会合并为一个翻译单元，以 JSON 数组形式分块发给模型分别翻译各块。这样既保持上下文连贯，又保证每块独立输出。结构块（标题/公式/图表）作为天然分隔符，不被合并。

### 表格翻译

表格被解析为 Markdown 表格格式，随后异步批量并发翻译。翻译内容为表头和单元格的英文文本，列分隔符 `|`、对齐行 `---`、行列结构完整保留。分组表头的结构通过 colspan/rowspan 展开为等宽 grid 后处理。

## 输出说明

翻译完成后在 `output/<arxiv_id>/` 下生成：

```
output/2605.26158v1/
├── 2605.26158v1.html          # 原始 HTML 备份
├── 2605.26158v1.zh.md         # 中文翻译 Markdown
├── 2605.26158v1.glossary.json # 术语表（可人工校对复用）
└── images/                    # 图片（仅当 IMAGE_LOCAL=true 时）
    ├── x1.png
    └── ...
```

Markdown 文件顶部包含论文标题、原文链接和翻译说明：

```markdown
# 论文中文标题

> 原文: https://arxiv.org/abs/2605.26158
> 本译文由 DeepSeek 自动翻译，公式与结构保留原文，仅供参考。

---

## 摘要
...
```

术语表 JSON 格式：

```json
{
  "alignment": {
    "zh": "对齐",
    "en_full": null,
    "note": "ML 领域指模型对齐，勿译"齐整/整齐"",
    "seen": true
  },
  "furina": {
    "zh": "<KEEP>",
    "en_full": null,
    "note": "框架名，勿译",
    "seen": true
  }
}
```

## 相关配置

本工具共用项目级配置（`.env` / 环境变量），无独立的工具配置。以下配置项与本工具最相关：

| 变量 | 说明 |
|------|------|
| `PAPER_TOOLS_CONCURRENCY` | 并发线程数，影响翻译速度 |
| `PAPER_TOOLS_IMG_LOCAL` | 是否下载图片到本地 |
| `PAPER_TOOLS_MERGE_MIN` | 短块合并阈值 |
| `PAPER_TOOLS_CITE_SEARCH` | 引用搜索引擎 |
| `PAPER_TOOLS_CITE_DISPLAY` | 引用显示模式 |

完整配置列表见 [项目 README](../../../README.md#配置)。

## 局限性

1. **仅支持 arxiv HTML 预览版**：依赖 ar5iv（LaTeXML）生成的 HTML 格式，约 80% 的 arxiv 论文有此版本。不支持的论文会下载失败。
2. **翻译质量受 LLM 影响**：依赖 DeepSeek 模型的翻译能力，极端专业领域可能需人工校对。
3. **需 API Key**：依赖 DeepSeek API，无本地离线翻译能力。
4. **表格翻译可能不完美**：复杂表格（合并单元格、分组头）的 Markdown 转换可能有信息损失，翻译时建议人工复核。

## License

MIT
