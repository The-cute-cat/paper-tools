# pdf-translate

将本地 PDF 格式论文经过两个阶段转为中文 Markdown。无需 arxiv HTML。

## 使用

在项目根目录执行：

```bash
uv sync
uv run python main.py pdf-translate "D:/papers/paper.pdf"
uv run python main.py pdf-translate "D:/papers/paper.pdf" --out ./output --dpi 180
uv run python main.py pdf-translate "D:/papers/paper.pdf" --extract-only
```

也可以像 arxiv 工具一样在 IDE 中直接运行（无需命令行参数）：右键
`paper_tools/tools/pdf_translate/main.py` -> Run / Debug，然后修改该文件
`if __name__ == "__main__":` 上方的常量：

```python
pdf_path = r"D:/papers/paper.pdf"  # 本地 PDF 路径
api_key = ""                       # 留空用 .env 的 DEEPSEEK_API_KEY
model = ""                         # 第二阶段翻译模型
vision_model = ""                  # 第一阶段视觉模型
out_dir = ""                       # 留空用项目根/output
dpi = 0                            # 0 = 用配置默认 160（范围 72-300）
max_output_tokens = 0              # 0 = 用配置默认 16384；密集页截断时调大
extract_only = False               # 只做第一阶段识别
resume = True                      # 复用逐页识别缓存
translate_skip = False             # 跳过第二阶段翻译
token_report = False               # 输出 token 用量与费用估算
```

该入口自行引导 `sys.path` 并初始化配置/日志，不依赖项目根目录的 `main.py`，
从任意工作目录运行均可。

`.env` 配置：

```dotenv
DEEPSEEK_API_KEY=你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_VISION_MODEL=deepseek-v4-flash-vision-exp
PAPER_TOOLS_PDF_DPI=160
PAPER_TOOLS_PDF_MAX_TOKENS=16384
```

`--model` 指定第二阶段翻译模型，`--vision-model` 指定第一阶段视觉模型。
`--dpi` 范围 72–300，默认 160；大尺寸页面自动限制到每边 4000 像素。
`--extract-only` 只运行第一阶段，**不是离线模式，仍需 API Key 并产生调用费用**。
`PAPER_TOOLS_SKIP_TRANSLATE=true` 对本工具也仅跳过第二阶段。
`--no-resume` 强制重新识别；默认自动复用校验有效的逐页缓存，不使用 arxiv 的交互恢复策略。
`--max-output-tokens` 设置视觉模型单页识别的最大输出 token（默认 16384，也可用
`PAPER_TOOLS_PDF_MAX_TOKENS`）。内容密集页（多公式/长表格）若响应被截断
（finish_reason=length），重试不会改变上限，必须调大本值——降低 DPI 对输出长度无效。

图片/请求体积超限（单图 > 32 MiB、请求 > 48 MiB、本页图 > 600 张）或输出截断时
会中止并在错误信息中提示修复方式；此前已识别的页面均已缓存，调整后重新运行即可续跑。

## 第一阶段：逐页识别

1. 使用 PyMuPDF 渲染完整页面，并生成四张重叠阅读细节图，以缓解视觉模型缩图造成的小字损失。
2. 从页面位图边界、矢量绘图区域生成插图候选裁剪，合并交叠区域，按 `p0001_img001` 编号。
3. 将整页、细节图、编号裁剪以及上页未完成内容一起发送给视觉模型。
4. 要求输出严格 JSON：`complete` 为已完整的 Markdown；`carry` 为留待下一页接续的原文；`ignored_images` 为表格、装饰等非插图候选编号。表格仍须输出为 Markdown。
5. 程序验证编号无重复或遗漏；当前图号必须被引用或明确忽略，上页待续内容中的图号不能丢弃。响应截断、无效 JSON、编号异常会重试，持续失败则停止，不静默产出不完整论文。
6. 下一页只携带 `carry`，已完成内容不会重复拼接。最后一页必须清空 `carry`，原稿残缺则保留文字并标记。
7. 将编号替换为相对路径图片链接，生成 `.extracted.md`。

逐页 JSON 保留输入待续内容、两个输出部分和缓存指纹。指纹包含 PDF 内容、页号、待续内容、模型、服务地址、DPI、输出上限及提取提示词。中断后重复运行即可复用有效页面。

## 第二阶段：翻译

复用 `LLMTranslator` 的学术翻译提示词、公式/引用保护与 `Glossary` 术语管理。
按 Markdown 段落边界分块；不从内部切开表格、多行公式和围栏代码。
顺序翻译并累计术语，使用论文开头作为全文上下文。图片链接、普通链接及代码以占位符保护；图注正常翻译。
检查公式、引用、图片占位符、标题层级及表格列数，失败时最多尝试三次；可用 `PAPER_TOOLS_TRANSLATE_REPAIR=false` 关闭重试，但不会关闭校验。

本工具第二阶段不使用 arxiv 的 HTML 解析、并发调度、DOCX/PDF 导出或翻译块断点缓存。
第二阶段中断后，原文和逐页识别缓存仍保留，重新运行会重新翻译全文。
单个超长段落/表格不会强拆，可能超出模型输出限制，应人工拆分处理。

## 输出

`output/<PDF文件名>-<内容哈希前12位>/`：

```text
paper.extracted.md      # 合并完成的原文
paper.zh.md             # 中文译文（第二阶段成功后写入）
paper.glossary.json      # 翻译术语表
images.json             # 候选图片编号与本地路径对应
images/                 # 编号图片裁剪（包含被模型忽略的候选，供核对）
pages/                  # 整页渲染及阅读细节图
extraction/             # 逐页 JSON 和恢复缓存
```

相同内容重复运行使用相同目录，并更新对应输出；同名但内容不同的 PDF 使用不同目录。
请将 Markdown 与 `images/` 一起移动，否则相对图片链接将失效。

## 限制与隐私

- 仅支持本地、非空、未加密 PDF，不支持直接输入下载 URL。
- 页面、裁剪及待译内容会发送至 `DEEPSEEK_BASE_URL` 指定的服务；不要处理无权上传的论文。
- 视觉提取不能保证零遗漏。公式、双栏阅读顺序、跨页表格和复杂版式需要人工复核。
- 插图候选采用 PDF 对象边界和矢量聚类，是启发式方法：不相连的复合图可能被拆成多个编号，复杂矢量图可能裁剪不完整。
- 扫描页通常只有一个整页位图，其中插图无法按 PDF 对象独立拆出；工具会警告，仍识别页面文字，但不保证保留独立插图。需要精确拆图时应预先做版面分割。
- 坐标一致性要求：工具按 PDF 存储方向渲染页面（忽略 `/Rotate` 旋转标记）。带旋转标记的扫描件可能以侧躺方向送入视觉模型，识别质量可能受影响；请预先「物理」旋转（重新导出）此类 PDF。
- 模型可以将候选判断为非插图；请核对 `ignored_images` 和页面图。程序能校验编号守恒，不能证明原文语义无遗漏。
- 第一阶段检查请求体和图片数量上限；过大时请降低 DPI。
- PyMuPDF 有 AGPL/商业许可选项，分发或商用集成前应自行确认依赖许可适用性。

视觉接口参数依据 [DeepSeek 官方视觉文档](https://api-docs.deepseek.com/guides/vision/)。

## 离线测试

```bash
uv run python -m unittest discover -s tests -v
```

测试使用临时 PDF 和模拟模型，不发送论文或消耗 API token。
