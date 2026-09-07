# md-export：Markdown → Word / PDF

通用的本地 Markdown 转 Word（.docx）/ PDF 工具，适用于技术笔记、博客、
翻译稿、论文等任意 Markdown 文档。对两类常见痛点做了重点处理：

1. **在线图片**：markdown 里引用的网络图片（`http(s)://...`）在 Word/PDF
   中无法直接使用，本工具会自动下载并内嵌到文档中。
2. **LaTeX 公式**：`$...$`、`$$...$$` 等公式在 Office 中无法直接显示，
   本工具把公式渲染为高清 PNG 后按排版位置嵌入（inline 随文、display 居中独立成行）。

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

# 2) 独立 IDE 入口（右键 Run，常量区填参数，同 pdf-translate）
python paper_tools/tools/md_export/main.py
```

代码调用（统一 `run` 接口）：

```python
from paper_tools.tools.md_export import run
run("paper.zh.md", fmt="docx_pdf", out_dir="./exports")
```

输入路径留空时回退到 `.env` 的 `PAPER_TOOLS_MD_INPUT`；
格式留空时回退到 `PAPER_TOOLS_MD_EXPORT_FORMATS`（默认 `docx_pdf`）。

## 支持的语法

| 元素 | 说明 |
|------|------|
| 标题 | `#` ~ `######`，映射为 Word 标题样式（可在 Word 中生成目录） |
| 公式 | `$inline$`、`$$display$$`、`\(...\)`、`\[...\]`、`\begin{equation}` 等，渲染为 PNG 内嵌 |
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

## 公式处理细节

- 公式由 matplotlib mathtext 渲染为透明背景 PNG（按内容哈希缓存在
  `<文件名>.md_export/math/`），无需系统安装 LaTeX。
- inline 公式以 0.55cm 基线高嵌入正文行内；display 公式按原始尺寸
  （限宽 16cm）单独成行居中。
- 部分复杂宏（mathtext 不支持的包、含中文的 `\text{}` 等）渲染失败时，
  退化为灰色斜体的 LaTeX 源码文本，保证内容不丢。
- 围栏代码块与行内代码中的 `$`、`\command` 不会被误判为公式。

## PDF 字形兼容

PDF 嵌入字体（宋体等 GBK 字体）缺少部分符号字形（✓ ✗ ☑ ➜ 等），
写入前会自动映射为等价字形（√ × →），避免显示为空白。

## 输出

- `<同名>.docx` / `<同名>.pdf`：位于 `--out` 目录（默认与源文件同目录）。
- `<文件名>.md_export/`：图片与公式的本地缓存目录，可安全删除（会自动重建）。
