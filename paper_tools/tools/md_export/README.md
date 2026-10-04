# md-export：Markdown → Word / PDF / 自包含 Markdown

通用的本地 Markdown 转换工具，可输出 Word（.docx）、PDF，以及**图片内联的
自包含 Markdown**。适用于技术笔记、博客、翻译稿、论文等任意 Markdown 文档。
对四类常见痛点做了重点处理：

1. **在线图片**：markdown 里引用的网络图片（`http(s)://...`）在 Word/PDF
   中无法直接使用，本工具会自动下载并内嵌到文档中。
2. **LaTeX 公式**：`$...$`、`$$...$$` 等公式在 Office 中无法直接显示，
   本工具把公式渲染为高清 PNG 后按排版位置嵌入（inline 随文、display 居中独立成行）。
3. **图表（diagram-as-code）**：主流 Markdown 编辑器（Typora 等）会原生渲染的
   三种围栏——```` ```mermaid ````、```` ```flow ````（flowchart.js）、
   ```` ```sequence ````（js-sequence-diagrams）——在原文里「本应是图」，
   直接导出却会显示源码。本工具会把它们渲染为图片后居中嵌入。
4. **分享单文件**：markdown 引用的图片是本地相对路径时，直接发给别人会缺图。
   `--format portable` 会把本地/网络图片统一转成 base64 内联进新的 markdown，
   得到一个单文件、对方直接打开即可看到全部图片的版本。

## 用法

与其他工具一致，提供三种入口：

```bash
# 1) 根 CLI 入口（与其他子命令一致）
#    默认同时导出 docx 与 pdf（与源文件同目录）
python main.py md-export paper.zh.md

#    只导出 Word / PDF / 两者
python main.py md-export paper.zh.md --format docx
python main.py md-export paper.zh.md --format pdf
python main.py md-export paper.zh.md --format all --out ./exports

#    生成可直接发给别人的自包含 markdown（图片内联为 base64）
python main.py md-export paper.zh.md --format portable

#    组合输出：同时导出 Word 与自包含 markdown
python main.py md-export paper.zh.md --format docx,portable

# 2) 独立 IDE 入口（右键 Run，常量区填参数，同 pdf-translate）
python paper_tools/tools/md_export/main.py
```

代码调用（统一 `run` 接口）：

```python
from paper_tools.tools.md_export import run
run("paper.zh.md", fmt="docx_pdf", out_dir="./exports")
run("paper.zh.md", fmt="portable")          # 自包含 markdown
```

输入路径留空时回退到 `.env` 的 `PAPER_TOOLS_MD_INPUT`；
格式留空时回退到 `PAPER_TOOLS_MD_EXPORT_FORMATS`（默认 `docx_pdf`）。
格式值可用逗号任意组合，`all` 等同于 `docx_pdf`（**不含** `portable`，
避免默认产出体积很大的内联文件）。

## 支持的语法

| 元素 | 说明 |
|------|------|
| 标题 | `#` ~ `######`，映射为 Word 标题样式（可在 Word 中生成目录） |
| 公式 | `$inline$`、`$$display$$`、`\(...\)`、`\[...\]`、`\begin{equation}` 等，渲染为 PNG 内嵌 |
| 图表 | ` ```mermaid `、` ```flow `（flowchart.js）、` ```sequence `（js-sequence-diagrams）渲染为 PNG 居中内嵌 |
| 图片 | `![alt](src)`、HTML `<img>`；本地相对/绝对路径，或网络 URL（自动下载内嵌） |
| 表格 | GFM 表格，首行加粗为表头，支持列对齐 `:---` / `:--:` / `---:`；cell 内公式同样渲染为图片 |
| 列表 | 有序/无序列表（两级嵌套）、任务列表 `- [ ]` / `- [x]` |
| 行内样式 | **粗体**、*斜体*、`行内代码`、~~删除线~~、[链接]（Word 中蓝色下划线） |
| 其他 | 引用块、围栏/缩进代码块、分隔线、行尾两空格硬换行、`<br>`、引用式链接 `[text][ref]` |
| 字体 | 中文正文字体可通过 `PAPER_TOOLS_MD_FONT` 配置（默认宋体） |

## 图片处理细节

- **网络图片**：按 URL 哈希缓存到源文件旁的 `<文件名>.md_export/images/`，
  重复转换不重复下载；下载复用全局配置的浏览器伪装头、CONNECT 代理
  （`PAPER_TOOLS_PROXY`）与重试策略。
- **负缓存**：确认 404（资源不存在）的 URL 会在缓存目录写入 `.miss` 标记，
  同一运行内及之后的转换不再重复请求；图片站点后来补图时删除对应
  `net_*.miss` 文件即可恢复尝试。网络类失败（超时/连接重置）只做单次
  运行内的会话级缓存，下次运行会自动重试。
- **SVG**：DOCX/PDF 均无法直接嵌入 SVG。转换时优先尝试同 URL 的
  `.png` / `.jpg` 位图（arxiv 等站点多数存在）；不存在时下载 SVG 并用
  svglib（依赖 `rlpycairo` 后端）以 2 倍尺寸栅格化为高清 PNG。
- **格式兼容**：webp / tiff 等 python-docx 不支持的格式会先用 Pillow 转成 PNG 再嵌入。
- **缩放**：图片等比缩放至页面宽度内（独立成图的图片最大 16cm），避免溢出。
- **图注**：`alt` 文本若非文件名，会作为居中灰色图注放在图片下方。
- 下载失败或文件缺失时，文档中以灰色 `[图片缺失: src]` 占位，不中断转换。

## 自包含 Markdown（`--format portable`）

把 markdown 里的图片统一转成 base64 data URI、以 `<img>` 标签写回，输出
`<同名>.portable.md`。适合把论文/笔记作为一个文件直接发给别人（微信、邮件、
聊天工具），对方无需同时拿到 `images/` 目录，也不依赖网络。

```bash
python main.py md-export paper.zh.md --format portable
```

处理范围：

| 输入写法 | 结果 |
|---------|------|
| `![alt](images/fig1.png)` | `<img src="data:image/png;base64,..." alt="alt" />` |
| `![alt](https://a.com/f.png "标题")` | 下载后内联，`title` 保留为同名属性 |
| `<img src="images/f.png" alt="x">` | 就地替换 `src` 为 data URI |
| `![alt](<路径 含空格.png>)` | 支持尖括号包裹的含空格路径 |
| 引用式 `![alt][ref]` + `[ref]: url` | 展开后内联（`[ref]: url` 定义行原样保留） |

几个刻意的设计：

- **只改图片，不动正文**。现有 docx/pdf 管线是单向且有损的解析（会丢 HTML
  标签、归一化文本、降级公式），复用它再生成 markdown 会改写正文。因此这里直接
  在源文本上定点替换，除图片引用外逐字节保持原样。
- **不碰代码**：围栏代码块（``` / ~~~）与行内代码 `` ` `` 内的 `![](...)` 示例
  不会被内联。
- **可重复执行**：已是 `data:` URI 的图片原样保留，重复运行不会二次编码。
- **图片解析复用同一套管线**：本地相对路径、网络下载、代理、重试、SVG 栅格化、
  404 负缓存都与 docx/pdf 共用 `<文件名>.md_export/images/` 缓存，不重复下载。
- **格式兼容**：浏览器不支持的格式（tiff 等）先用 Pillow 转 PNG。
- **SVG 默认栅格化**，可用 `PAPER_TOOLS_MD_PORTABLE_KEEP_SVG=1` 改为保留矢量：
  - 栅格化（默认，兼容性优先）：`data:image/svg+xml` 属于「活动内容」（可含脚本），
    带 HTML 消毒的渲染环境可能把它过滤掉，PNG 更保险。
  - 保留矢量（清晰度优先）：线条锐利、放大不糊，且**体积通常小一到两个数量级**
    （实测一张简单矢量图：28.7 KB → 0.4 KB）。适合确定接收方用 Typora /
    VS Code / Obsidian 这类浏览器内核预览的场景。
  - 拿不准时：做一份同样的图分别用 PNG / SVG 两种 data URI 的 A/B 小文件让对方
    打开看一眼，比查资料快。注意本项只影响 `portable`，docx/pdf 无法嵌入 SVG，
    始终栅格化。
- **失败不中断**：图片找不到或读取失败时保留原引用并在日志中列出，其余图片照常内联。
- **体积提示**：base64 会让内容膨胀约 1/3，单个 data URI 超过 64 KB、或整个文件
  超过 5 MB 时，日志给出提示。

### 超长 data URI 的渲染器差异

不同渲染器对**单个 data URI 长度**的容忍度差别很大，这属于渲染器自身的限制，
不是图片损坏，因此本工具**只告警、不压缩**，保持原图画质：

| 渲染器 | 实测表现 |
|--------|---------|
| Typora | 64 KB 正常渲染；**256 KB 起解析失败**——整段 `<img src="data:...">` 被当作纯文本显示（base64 裸露在正文里） |
| 其他编辑器 | 据反馈通常可正常显示（未逐项实测） |

日志会在超过 64 KB 时提示张数与最长值，例如：

```
[WARNING]   2 张图片的 data URI 超过 64 KB（最长 319 KB）：部分渲染器
            （实测 Typora 256 KB 起）会拒绝渲染……
```

如果接收方用的是 Typora 这类渲染器，处理办法是**先压缩图片**（缩小尺寸或
转 JPEG/WebP）再转换，让每张图的 data URI 落在阈值内。

> 说明：`portable` 只处理图片，不处理公式与图表。若接收方的渲染器不支持
> `$...$` 或 ```` ```mermaid ````，它们仍会显示为源码（docx/pdf 会把公式和
> 图表渲染成图片）。

## 公式处理细节

- 公式由 matplotlib mathtext 渲染为透明背景 PNG（按内容哈希缓存在
  `<文件名>.md_export/math/`），无需系统安装 LaTeX。
- inline 公式以 0.55cm 基线高嵌入正文行内；display 公式按原始尺寸
  （限宽 16cm）单独成行居中。
- 部分复杂宏（mathtext 不支持的包、含中文的 `\text{}` 等）渲染失败时，
  退化为灰色斜体的 LaTeX 源码文本，保证内容不丢。
- 围栏代码块与行内代码中的 `$`、`\command` 不会被误判为公式。

## 图表（Mermaid / flowchart.js / js-sequence）细节

只处理**主流 Markdown 编辑器会原生渲染**的三种围栏，因为只有它们在原文里
「本应是图」：

| 围栏 | 语法 | 会被谁渲染 |
|------|------|-----------|
| ```` ```mermaid ````（或 `mmd`） | Mermaid | Typora、Obsidian、GitHub、GitLab、Notion，VS Code（装 Mermaid 扩展） |
| ```` ```flow ```` | flowchart.js | Typora、Mark Text |
| ```` ```sequence ```` | js-sequence-diagrams | Typora、Mark Text |

> 其他「图即代码」语言（PlantUML、Graphviz、D2、Vega-Lite 等）这些编辑器
> 都不渲染，在 markdown 里通常就是普通代码块，因此**刻意不处理**，按代码块输出。

这些图都要靠浏览器 JS 才能画出来，Python 无法直接渲染，因此 `flow` /
`sequence` 会先**机械转译成 Mermaid**（两者语法分别源自 Mermaid / PlantUML 系），
再复用同一套渲染后端。后端由 `PAPER_TOOLS_MD_MERMAID` 选择：

| 取值 | 行为 | 适用场景 |
|------|------|---------|
| `auto`（默认） | 系统 PATH 上有 `mmdc` 就用本地渲染，否则用在线服务 | 无需配置，开箱即用 |
| `local` | 仅用本地 mermaid-cli（`mmdc`）；缺失时退回 `npx -y @mermaid-js/mermaid-cli` | 离线、涉密、内网文档 |
| `online` | 仅用在线渲染服务（`PAPER_TOOLS_MD_MERMAID_ENDPOINT`，默认 mermaid.ink） | 机器上没有 Node.js |
| `off` | 关闭，三种围栏都按普通代码块原样输出 | 不希望图表被替换 |

- **在线渲染有隐私代价**：图表源码会发送到第三方服务（默认 mermaid.ink，
  采用 deflate 压缩后拼接在 URL 里）。涉密或不外传的文档请用 `local` 或 `off`。
- **本地渲染**需 Node.js 与 `@mermaid-js/mermaid-cli`（`npm i -g @mermaid-js/mermaid-cli`）。
  自定义路径可设 `PAPER_TOOLS_MD_MERMAID_MMDC`；`local` 模式下若没有 `mmdc`
  会尝试 `npx`（首次会联网下载依赖，较慢）。本地渲染默认加 `--no-sandbox`
  （Windows / 容器里 Chromium 常因沙箱崩溃）。
- **清晰度**：在线渲染默认按 `PAPER_TOOLS_MD_MERMAID_WIDTH`（1600px）请求，
  本地渲染按其换算的 scale 放大，保证插入文档后不糊。
- **主题**：`PAPER_TOOLS_MD_MERMAID_THEME` 可设 `default` / `neutral` /
  `dark` / `forest` / `base`；留空用 Mermaid 默认主题。
- **渲染结果按内容哈希缓存**到 `<文件名>.md_export/diagram/`，docx 与 pdf
  两轮渲染只算一次，重复转换不重复请求。
- **失败不中断**：语法错误、转译后 Mermaid 解析失败、服务不可达、未装
  mermaid-cli 等情况只记录 warning，该代码块退化为普通代码块输出，其余内容
  照常转换。
- **转译是有损的**：`flow` / `sequence` 转成 Mermaid 后样式与原编辑器略有差异
  （节点形状/配色按 Mermaid 默认），但语义与连线一致。若某个图转译后渲染失败，
  会原样输出源码而不是产出错图。
- **portable 不处理图表**：与公式一致，`--format portable` 只内联图片，
  三种围栏的代码块原样保留（接收方渲染器能否画图取决于其自身支持）。

## PDF 字形兼容

PDF 嵌入字体（宋体等 GBK 字体）缺少部分符号字形（✓ ✗ ☑ ➜ 等），
写入前会自动映射为等价字形（√ × →），避免显示为空白。

## 输出

- `<同名>.docx` / `<同名>.pdf` / `<同名>.portable.md`：位于 `--out` 目录
  （默认与源文件同目录），按 `--format` 选择生成。
- `<文件名>.md_export/`：图片、公式与 Mermaid 渲染的本地缓存目录，可安全删除（会自动重建）。
