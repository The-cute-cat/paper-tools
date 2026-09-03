# arxiv-translate

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)

将 arxiv 论文的 HTML 预览版（ar5iv 格式）下载并翻译为中文 Markdown，保留公式、图表、表格、引用等所有结构。

## 功能概览

- 输入 arxiv 链接或 ID，自动下载 HTML 预览版
- 解析论文结构：标题、章节、段落、公式、图表、列表、表格、引用、文本框/提示框
- 公式完整保护（LaTeX 占位符机制）：翻译时公式以占位符保护不被改写，同时公式原文作为上下文发给 LLM 以理解语义、提升翻译准确性
- 自动构建术语表，全文术语锁定，消除前后译法不一致
- 引用智能处理：论文引用转为搜索引擎链接（论文名作 hover 提示），方便查阅
- 文本框/提示框保留：定理、备注、Prompt 示例等带边框文本框以引用块（`> `）形式保留，强调标记不丢失
- 图表结构保留：表格按单元格翻译，图片可选本地下载
- 原文模式：可跳过 LLM，仅解析并导出结构化英文原文（无需 API Key）
- 断点续译：异常退出后自动/手动恢复已翻译内容，避免从头重翻
- 网络代理：支持标准代理与轻量 CORS 文本转发代理，适配受限网络
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
MERGE_MIN_CHARS = 1000    # 翻译单元目标长度下限（字符）：相邻同类文本块会被贪心
                            # 凑成 [MERGE_MIN_CHARS, MERGE_TARGET_MAX] 区间的单元，一起
                            # 以 JSON 分块翻译；设为 0 关闭合并。
MERGE_TARGET_MAX = 1500    # 翻译单元目标长度上限（字符）：凑单元时累计达此值即关闭
                            # 当前单元；单块 ≥ 此值则独立成单元（不强行拆分）；设为 0 不限制。
EXPORT_FORMATS = ""        # 额外导出格式（DOCX / PDF，实验中），逗号分隔如 "docx,pdf"
OUTPUT_MARKDOWN = True     # 是否仍输出 .zh.md；导出额外格式时设为 False 可只产出导出格式
TRANSLATE_SKIP = False     # 跳过翻译：True 时不调用 LLM，仅解析并输出论文英文原文（结构化原文 markdown）
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
  ├─ 公式 → 占位符保护
  ├─ 文本 → 术语约束 + 立场锚定
  ├─ 表格 → 异步批量翻译
  └─ 短块合并 → JSON 分块翻译（合并组各子块独立回收）
    │
    ▼
一致性检查与返修（按翻译单元判定，合并组任一子块不通过则整组重翻）
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

**公式原文也作为上下文发给 LLM**：翻译单元会附带一张「公式表」（每项含 `id` / `latex` / `display`），仅作为输入上下文提供给模型，帮助它结合公式语义理解变量名、区分"上升/最大/最小化"等含义，从而让译文更符合数学本意。该表**仅用于辅助判断、不要求模型回写**——模型只需在译文中照抄对应的 `⟦MATH_<id>⟧` 占位符，最终由 `restore_math` 用公式表精确还原。这样既保住了公式不被篡改，又借助公式语义提升了翻译准确性。

### 术语表（Glossary）

术语表的构建与使用分三个阶段，全程由论文自身文本驱动，不依赖任何硬编码术语：

1. **建表阶段（单线程）**：先注入领域默认术语种子（`Glossary.with_defaults`，如领域通用词），再取"标题 + 摘要及前几段（至多 4 段）"作为种子文本发一次翻译，让模型补充自创词/易错词译法并 `ingest` 进表。锁定自创方法/框架名（如 `Furina` → 保留原文 `KEEP`）和上下文敏感术语（如 `agent` → 智能体，而非"代理"）。
2. **缩写定义预热（单线程）**：先扫描英文原文中的 `ABBR = English` 缩写定义入库（仅记录 缩写→英文全称）；再找出含缩写定义的块——图注/表注/伪代码注，以及正文里出现 `ABBR = English` 的段落——单线程优先翻译这些块，将其译法 `ingest` 进术语表。这些块在后续并发阶段直接复用、不再重翻，从而保证表格表头与图注里的同一缩写译法一致。
3. **并发与合并阶段**：翻译每块后，模型返回本块确定的新术语，自动合并进共享术语表；缩写预热阶段锁定的译法全程优先。

每条术语记录：
- **英文 key**：小写归一化的英文原文（缩写定义另记英文全称）
- **中文译法**：锁定译法或 `<KEEP>` 标记（保留英文）
- **英文全称**（可选）：缩写对应的完整形式
- **备注**（可选）：如"论文自创框架名，勿译"

最终术语表保存为 JSON，可人工校对复用。

### 论文立场摘要（防幻觉/串文）

为保证全文立场一致、抑制模型幻觉，翻译时向 prompt 注入一句论文全局立场摘要。该摘要为**零成本实现**：直接用"标题 + 摘要前几段（至多 3 段，超 600 字截断）"拼成一句简短说明，要求模型据此判断核心任务/方法/立场并保持一致；摘要缺失时回退到标题，再缺失时仅提示"保持立场一致"。摘要仅作为内部 prompt 上下文，不向用户控制台输出全文。

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

检测不通过则返修。返修以"翻译单元"为粒度判定：合并组中任一子块不通过检查，即把整组合并组重新发送给模型翻译，直到整组通过检查或达到最大尝试次数（`PAPER_TOOLS_TRANSLATE_REPAIR` 关闭则跳过此阶段）。

### 短块合并

相邻的同类文本块（段落 / 列表项 / 章节标题）会被贪心合并为"翻译单元"，目标是让每个单元落入 `[MERGE_MIN, MERGE_MAX]` 字符区间（默认 `1000` / `1500`，由 `PAPER_TOOLS_MERGE_MIN` / `PAPER_TOOLS_MERGE_MAX` 控制；设 `MERGE_MIN=0` 关闭合并）。

- 合并时按块类型分组：仅**同类型**块可并入同一单元，标题、公式、图表、表格等结构块作为天然分隔符，不被合并。
- 单块文本长度已达 `MERGE_MAX` 时不强行拆分，独立成单元。
- 合并组以 JSON 数组形式分块发给模型，模型分别回收各块译文（文本互不拼接），既保持上下文连贯，又保证每块独立输出。
- 翻译与返修均按"翻译单元"调度：合并组内任一子块需返修则整组重翻。

### 表格翻译

表格被解析为 Markdown 表格格式，随后异步批量并发翻译。翻译内容为表头和单元格的英文文本，列分隔符 `|`、对齐行 `---`、行列结构完整保留。分组表头的结构通过 colspan/rowspan 展开为等宽 grid 后处理。

### Token 用量与费用估算

翻译器在线程安全的 `TokenUsage` 中累计每次 API 调用的 token 消耗（输入、输出、缓存命中/未命中、请求次数）。开启 `PAPER_TOOLS_TOKEN_REPORT`（或在 IDE 常量中设 `TOKEN_REPORT = True`）后，翻译结束会在日志输出：

- 总输入 / 输出 / 合计 token
- 缓存命中量与占比、未命中量与占比
- 依据 DeepSeek 官方价目表估算的费用

费用按 `缓存命中输入/1e6 × 命中单价 + 未命中输入/1e6 × 未命中单价 + 输出/1e6 × 输出单价` 计算。价目表不写死在代码里，由 `paper_tools/core/pricing.py` 在运行时从官方定价页动态抓取并本地缓存（`pricing_cache.json`，默认 24h 有效）；抓取失败时回退到最近一次缓存。

### 原文模式（仅解析不翻译）

开启 `PAPER_TOOLS_SKIP_TRANSLATE=1`（或 IDE 常量 `TRANSLATE_SKIP=True`）后，工具不调用 LLM，仅把 ar5iv HTML 解析为结构化 Markdown 并输出论文英文原文（文件名后缀 `.en.md`）。适用于只想拿到带公式/图表/引用链接的结构化原文、或排查解析问题的场景；此模式下无需配置 `DEEPSEEK_API_KEY`，也不生成术语表与 `.zh.html`。

### 断点续译

翻译过程中若异常退出，下次运行会检测到上次残留的翻译缓存。行为由 `PAPER_TOOLS_RESUME_MODE` 控制：

- `ask`（默认）：在交互终端询问 恢复(r) / 重新翻译(n) / 退出(q)；
- `auto`：自动恢复，跳过已翻译的块（适合 CI / 无交互环境）；
- `never`：总是从头翻译（忽略缓存，启动即删除）。

非交互终端（CI、重定向）下若设为 `ask` 会自动退化为 `auto`，避免卡死。

### 网络代理

部分网络对 arxiv.org 在 TLS 握手阶段直接 RST（伪装请求头无法绕过），需走代理：

- `PAPER_TOOLS_PROXY`：标准 CONNECT 隧道代理（如 `http://127.0.0.1:7890`），文本与图片二进制下载都经它；也兼容 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量。
- `PAPER_TOOLS_CORS_PROXY`：轻量 CORS 文本转发代理（如 `https://your-worker.dev/?url=`）。仅用于文本/HTML 下载（abs 页 + 全文），不支持二进制；图片下载仍走 `PAPER_TOOLS_PROXY`。适用于不支持 CONNECT 隧道但有服务侧出网能力的场景。

## 输出说明

翻译完成后在 `output/<arxiv_id>/` 下生成：

```
output/2605.26158v1/
├── 2605.26158v1.html          # 原始 HTML 备份
├── 2605.26158v1.zh.md         # 中文翻译 Markdown（若开启额外导出格式且 OUTPUT_MD=false 则可能不生成）
├── 2605.26158v1.glossary.json # 术语表（可人工校对复用）
├── 2605.26158v1.zh.html       # 中文 HTML（中间产物，用于导出 docx/pdf）
├── 2605.26158v1.zh.docx       # 额外导出（仅当 EXPORT_FORMATS 含 docx）
├── 2605.26158v1.zh.pdf        # 额外导出（仅当 EXPORT_FORMATS 含 pdf）
└── images/                    # 图片（仅当 IMAGE_LOCAL=true 时）
    ├── x1.png
    └── ...
```

另外，价目表缓存 `paper_tools/core/pricing_cache.json` 由价格模块在首次需要时生成（非每篇论文重建），用于 token 费用估算，与单篇输出无关。

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
| `PAPER_TOOLS_DL_RETRIES` | 图片等二进制下载失败时的重试次数 |
| `PAPER_TOOLS_PROXY` | 下载代理（标准 CONNECT 隧道，支持二进制），如 `http://127.0.0.1:7890` |
| `PAPER_TOOLS_CORS_PROXY` | 轻量 CORS 文本转发代理（仅文本/HTML 下载），如 `https://worker.dev/?url=` |
| `PAPER_TOOLS_MERGE_MIN` | 翻译单元目标长度下限（字符，0=关闭） |
| `PAPER_TOOLS_MERGE_MAX` | 翻译单元目标长度上限（字符，0=不限制） |
| `PAPER_TOOLS_CITE_SEARCH` | 引用搜索引擎 |
| `PAPER_TOOLS_CITE_DISPLAY` | 引用显示模式 |
| `PAPER_TOOLS_NAME_MODE` | 输出文件命名方式（id/title/title_zh） |
| `PAPER_TOOLS_TOKEN_REPORT` | 翻译后输出 token 用量与费用估算（1/true 开启） |
| `PAPER_TOOLS_EXPORT_FORMATS` | 额外导出格式（docx/pdf/docx_pdf/all，实验中） |
| `PAPER_TOOLS_OUTPUT_MD` | 导出额外格式时是否仍输出 `.zh.md` |
| `PAPER_TOOLS_SKIP_TRANSLATE` | 跳过翻译，仅输出英文原文（1/true；开启后无需 API Key） |
| `PAPER_TOOLS_INPUT` | 待翻译的 arxiv 链接或 ID（命令行/INPUT 未提供时回退） |
| `PAPER_TOOLS_SUMMARY_MAX_CHARS` | 立场摘要截断上限（字符，0=不截断） |
| `PAPER_TOOLS_RESUME_MODE` | 断点续译模式：ask / auto / never |

完整配置列表见 [项目 README](../../../README.md#配置)。

## 局限性

1. **仅支持 arxiv HTML 预览版**：依赖 ar5iv（LaTeXML）生成的 HTML 格式，约 80% 的 arxiv 论文有此版本。不支持的论文会下载失败。
2. **翻译质量受 LLM 影响**：依赖 DeepSeek 模型的翻译能力，极端专业领域可能需人工校对。
3. **需 API Key**：依赖 DeepSeek API，无本地离线翻译能力。
4. **表格翻译可能不完美**：复杂表格（合并单元格、分组头）的 Markdown 转换可能有信息损失，翻译时建议人工复核。

## License

MIT
