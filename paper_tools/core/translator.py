"""翻译器：封装 DeepSeek (OpenAI 兼容) API，可被任意工具复用。"""

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml
from openai import OpenAI

from ..config import LLMSettings, get_settings
from .glossary import Glossary, KEEP_AS_IS

_log = logging.getLogger("paper-tools")

# 行内 $...$ 与行间 $$...$$ 公式保护：翻译前替换为占位符，翻译后还原，
# 避免模型把长公式当文本改写或注入乱码。
# 额外保护"裸 LaTeX"（未用 $ 包裹的 \command{...} 序列），常见于 arxiv 解析后
# 行内公式包裹类名缺失的场景（如某些 ltx_Math 嵌套在特殊 tag 中）。
_MATH_BLOCK_RE = re.compile(r"\$\$.*?\$\$", re.DOTALL)
# 行内 $...$：所有 inline 公式在一遍扫描中按从左到右精准保护。
_MATH_INLINE_RE = re.compile(r"\$[^$\n]+?\$")
# 前缀合并（如 H$_{\text{tok}}$ → $H_{\text{tok}}$）已移至 parser 的
# _plain_text_for_translation 中处理，translator 不再做前置合并。
# 裸 LaTeX（未用 $ 包裹的 \command{...} 序列），兜底处理 arxiv 解析后残留。放到最后。
#
# 设计：parser 已用 wrap_math=True 把所有 inline 公式包成 $...$ 喂进来，
# 此正则只是兜底（少数 ltx_Math 因嵌套在特殊 tag 中未包成 $...$）。
# 因此采用"严格无空格链式 LaTeX"匹配：只匹配 \command{arg} 或 \command 的
# 紧密串联（中间允许 {} 不允许空格），避免把 "\mathcal{S} denotes" 这种
# "LaTeX 命令 + 普通词" 的混合串误吞为单个公式。
#   - \theta              ✓
#   - \mathcal{Y}         ✓
#   - \mathcal{Y}\to      ✓ （链式）
#   - \mathcal{S} denotes ✗ （中间有空格，不该吃）
#   - \theta define       ✗ （中间有空格，不该吃）
_MATH_BARE_RE = re.compile(
    r"\\[a-zA-Z]+(?:\{[^{}\n]*(?:\{[^{}\n]*\}[^{}\n]*)*\})?(?:\\[a-zA-Z]+(?:\{[^{}\n]*\})?)*"
)
# 指标+箭头+LaTeX 混合脏写法（如 HD↓max{}_{\max}\downarrow、RD↑...，或 LaTeX 箭头 ASR\uparrow），
# 整体保护，避免箭头/下标/downarrow 被分别翻译打乱。
_METRIC_DIRTY_RE = re.compile(
    r"\b[A-Z][A-Za-z0-9_]*[↑↓][^\s,)]*"
    r"|\b[A-Z][A-Za-z0-9_]*\\(?:up|down)arrow[^\s,)]*"
)
# 简单指标变化表达式，例如 ASR↑、Htok ↑、HDmax ↓、RDmax ↓。
_METRIC_CHANGE_RE = re.compile(r"\b[A-Z][A-Za-z0-9_]*\s*[↑↓]")
# 参考文献引用占位符（[18]、[1, 2]、[18, 19] 等）。ar5iv 中这类数字紧贴
# 模型名（GPT-5[18]、Claude 4[1]），LLM 翻译时容易把 [18] 当作"模型名的一部
# 分"拼出"GPT-5 18"这种错误译法。保护后送入 LLM，由它原样照抄占位符，
# restore 时再还原为原始 [数字] 字符串。
#
# 占位符形态选择：使用 ⟦REF_Xn⟧ 这种「带 X 前缀 + 编号」的格式，而不是
# ⟦REF_n⟧。原因：LLM 看到 ⟦REF_n⟧ 时会按真实引用编号「脑补」改写（如把
# ⟦REF_0⟧ 改成 ⟦REF_18⟧），导致 restore 时索引对不上，占位符原样残留。
# 「X」前缀是真实论文中绝不可能出现的字符（真实引用只有数字），LLM 不会
# 把它当成真实编号改写，也就不会破坏占位符结构。
_REFERENCES_RE = re.compile(r"\[(\d+(?:[\s,\-\u2013\u2014]\d+)*)\]")
# 使用 ⟦MATH_n⟧ / ⟦REF_Xn⟧ 而非 <MATH_n>，避免被模型误当成 HTML/Markdown 标签而吃掉或改写。
_MATH_PH_RE = re.compile(r"⟦MATH_(\d+)⟧")
_REF_PH_RE = re.compile(r"⟦REF_X(\d+)⟧")
_MATH_PH_FMT = "⟦MATH_{}⟧"
_REF_PH_FMT = "⟦REF_X{}⟧"

# ---------- 提示词模板：从 YAML 加载，不硬编码在代码中 ----------
_PROMPT_PATH = Path(__file__).resolve().parent / "translator_prompts.yaml"


@lru_cache(maxsize=1)
def _load_prompts() -> dict[str, Any]:
    """加载提示词 YAML 模板（模块级单例缓存）。"""
    with open(_PROMPT_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass
class TokenUsage:
    """累计 token 用量统计（线程安全，供并发翻译共享同一翻译器实例时累加）。

    字段含义：
      - prompt_tokens      : 输入 token 总量（含 system + user）
      - completion_tokens  : 输出 token 总量（模型生成部分）
      - cache_hit_tokens   : 命中提示缓存的输入 token（DeepSeek 返回
                             usage.prompt_tokens_details.cached_tokens）
      - cache_miss_tokens  : 未命中缓存的输入 token = prompt - cache_hit
      - requests           : API 调用次数（用于诊断）
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    requests: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cache_miss_tokens(self) -> int:
        return max(0, self.prompt_tokens - self.cache_hit_tokens)

    @classmethod
    def from_response(cls, usage: Any) -> "TokenUsage":
        """从 OpenAI/DeepSeek 响应的 usage 对象构造单次用量（不累加）。"""
        if usage is None:
            return cls()
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        cache_hit = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cache_hit = int(getattr(details, "cached_tokens", 0) or 0)
        return cls(
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache_hit_tokens=cache_hit,
            requests=1,
        )

    def add(self, other: "TokenUsage") -> None:
        """线程安全累加单次用量。"""
        with self._lock:
            self.prompt_tokens += other.prompt_tokens
            self.completion_tokens += other.completion_tokens
            self.cache_hit_tokens += other.cache_hit_tokens
            self.requests += other.requests

    def report_lines(self) -> list[str]:
        """生成可读的用量报告行（供 logger 输出）。"""
        pt = self.prompt_tokens
        ct = self.completion_tokens
        hit = self.cache_hit_tokens
        miss = self.cache_miss_tokens
        total = self.total_tokens
        pct = (hit / pt * 100) if pt else 0.0
        miss_pct = (miss / pt * 100) if pt else 0.0
        write_pct = (ct / total * 100) if total else 0.0
        return [
            "────────── Token 用量报告 ──────────",
            f"  API 调用次数      : {self.requests}",
            f"  输入 token 总量  : {pt:,}",
            f"    命中缓存(读)    : {hit:,}  ({pct:5.1f}%)",
            f"    未命中(读)      : {miss:,}  ({miss_pct:5.1f}%)",
            f"  输出 token 总量  : {ct:,}  ({write_pct:5.1f}% of total)",
            f"  合计 token        : {total:,}",
            "────────────────────────────────────",
        ]


class LLMTranslator:
    """基于配置的翻译器。公式以 $...$ / $$...$$ 形式保留原样。"""

    def __init__(self, llm: Optional[LLMSettings] = None):
        self.llm = llm or get_settings().llm
        if not self.llm.api_key:
            raise ValueError(
                "未提供 API key。请设置环境变量 DEEPSEEK_API_KEY，"
                "或在 .env 文件中配置，或显式传入 llm 参数。"
            )
        self._client = OpenAI(
            api_key=self.llm.api_key,
            base_url=self.llm.base_url,
            timeout=self.llm.timeout,
            max_retries=self.llm.max_retries,
        )
        # token 用量累计（线程安全，并发翻译共享该实例时累加）
        self.usage = TokenUsage()

    # ---------- 公式/引用占位符保护 ----------
    @staticmethod
    def protect_math(text: str) -> tuple[str, list[str], list[str]]:
        """将公式与参考文献引用替换为 ⟦MATH_n⟧ / ⟦REF_n⟧ 占位符。

        返回 (保护后文本, 公式列表, 引用列表)。公式列表索引 n 对应 ⟦MATH_n⟧；
        引用列表索引 n 对应 ⟦REF_n⟧。两个列表分别管理、互不干扰。
        """
        formulas: list[str] = []

        def _sub_block(m):
            formulas.append(m.group(0))
            return _MATH_PH_FMT.format(len(formulas) - 1)

        protected = _MATH_BLOCK_RE.sub(_sub_block, text)
        # 保护行内 $...$ 公式
        protected = _MATH_INLINE_RE.sub(_sub_block, protected)
        # 指标+箭头+LaTeX 混合脏写法整体保护（如 HD↓max{}_{\max}\downarrow）
        protected = _METRIC_DIRTY_RE.sub(_sub_block, protected)
        # 简单指标变化表达式（ASR↑、HDmax ↓ 等）
        protected = _METRIC_CHANGE_RE.sub(_sub_block, protected)
        # 兜底：残留的裸 LaTeX 命令（最后处理，避免与公式/指标规则冲突）
        protected = _MATH_BARE_RE.sub(_sub_block, protected)

        # 保护参考文献引用 ⟦REF_n⟧（[18]、[1, 2]、[18-20] 等）。这一步放在所有
        # _MATH_*_RE 之后，避免误把"[$...$]"类切片当公式吞噬。_REFERENCES_RE
        # 只匹配 [数字] 这种紧凑格式，不会与公式规则冲突。
        references: list[str] = []

        def _sub_ref(m):
            references.append(m.group(0))
            return _REF_PH_FMT.format(len(references) - 1)

        protected = _REFERENCES_RE.sub(_sub_ref, protected)

        # ── 防御性自检：占位符之外不应残留 $ ──
        cleaned = re.sub(r"⟦(?:MATH|REF)_\d+⟧", "", protected)
        dangling = re.findall(r"\$+", cleaned)
        if dangling:
            _log.warning(
                "protect_math: found %d dangling '$' outside placeholders. "
                "This may indicate a parser bug producing unbalanced math delimiters. "
                "Dangling: %s",
                len(dangling),
                dangling[:6],
            )

        return protected, formulas, references

    @staticmethod
    def restore_math(text: str, formulas: list[str], references: Optional[list[str]] = None) -> str:
        """将 ⟦MATH_n⟧ 还原为公式，将 ⟦REF_n⟧ 还原为参考文献引用串。

        同时对还原出的公式逐个做 KaTeX 兼容性清洗：原 arXiv PDF 源码可能
        包含 KaTeX 不支持/兼容性差的 LaTeX 命令（如 \\textsc{}——small caps，
        KaTeX 不支持会报 Undefined control sequence），必须在落盘前替换为
        KaTeX 支持的等价命令，否则最终 markdown 在 Typora 等基于 KaTeX 的
        渲染器中整段 $$ 块无法显示。
        """
        references = references or []

        def _sanitize_one(formula: str) -> str:
            # 与 _sanitize_bare_latex 中"模式 4"对齐。
            # 注意：原 \\textsc 后是大写单词（如 Unsafe/Safe），非贪婪匹配 {…}。
            return re.sub(r"\\textsc\{", r"\\text{", formula)

        # 还原公式
        def _sub_math(m):
            idx = int(m.group(1))
            if 0 <= idx < len(formulas):
                return _sanitize_one(formulas[idx])
            return m.group(0)
        text = _MATH_PH_RE.sub(_sub_math, text)
        # 还原引用
        def _sub_ref(m):
            n = int(m.group(1))
            return references[n] if 0 <= n < len(references) else m.group(0)
        return _REF_PH_RE.sub(_sub_ref, text)

    @staticmethod
    def _sanitize_bare_latex(text: str) -> str:
        """后处理：修复 LLM 在译文里自行生成的裸 LaTeX 残骸。

        尽管 prompt 明确要求保留 ⟦MATH_n⟧ 占位符，模型有时仍会无视占位符，
        把公式写成"半 Unicode 半 LaTeX"的混合体（如 HD↓max{}_{\\max}\\downarrow）。
        此方法在 restore_math 之后运行，对已保护公式之外的残骸做兜底修复。
        """
        # 模式 0: \\thicksim → \\sim（KaTeX 不支持 \\thicksim，必须放在 $...$ 保护之前）
        if "\\thicksim" in text:
            text = text.replace("\\thicksim", "\\sim")

        safe: list[str] = []

        def _protect(m):
            safe.append(m.group(0))
            return f"⟦SAFE_{len(safe) - 1}⟧"

        # 先保护已有的正确 $...$ / $$...$$ 块，避免误改
        text = _MATH_BLOCK_RE.sub(_protect, text)
        text = _MATH_INLINE_RE.sub(_protect, text)

        dirty = False

        # 模式 1: HD↓max{}_{\max}\downarrow / RD↓max{}_{\max}\downarrow 等
        #   xxx↓/↑YYY{}_{ZZZ}\downarrow/\uparrow → $xxx_{ZZZ}\downarrow/\uparrow$
        p1 = re.compile(
            r"\b([A-Z][A-Za-z]*)([↓↑])([a-zA-Z0-9]+)\{}_{([^}]+)\}\\(down|up)arrow"
        )
        if p1.search(text):
            dirty = True
            text = p1.sub(r"$\1_{\4}\\\5arrow$", text)
            # 关键：模式 1 修复后新产生了 $...$ 块，立即重新保护，
            # 否则后续模式 2/3 会对这些新块内的 \arrow 等再匹配
            text = _MATH_INLINE_RE.sub(_protect, text)

        # 模式 2: 残留的裸 \downarrow / \uparrow（不在 $...$ 内）
        p2 = re.compile(r"(?<!\$)\\(down|up)arrow")
        if p2.search(text):
            dirty = True
            text = p2.sub(r"$\\\1arrow$", text)

        # 模式 3: 残留的裸 {}_{...} 或 {}^{...}（如 {}_{\max}）
        p3 = re.compile(r"(?<!\$)\{\}([_^]\{[^}]+\})")
        if p3.search(text):
            dirty = True
            text = p3.sub(r"$\1$", text)

        # 模式 4: KaTeX 不支持/兼容性差的命令（LLM 在译文中自造）→ 替换为 KaTeX 等价物
        #   - \textsc{...}: small caps，KaTeX 不支持，会报
        #     "Undefined control sequence: \textsc"。降级为 \text{...}（KaTeX 支持）
        #     以保住公式的渲染，避免整段 $$ 块因单个控制序列报错而显示原始 LaTeX。
        #   这里只替换已保护块**之外**的残留。前面 _MATH_BLOCK_RE /
        #   _MATH_INLINE_RE 已把完整 $...$ 块替换成 ⟦SAFE_n⟧，所以剩下都是
        #   LLM 在行间或前后文本里额外造的命令。
        p4 = re.compile(r"\\textsc(\{)")
        if p4.search(text):
            dirty = True
            text = p4.sub(r"\\text\1", text)

        # 恢复已保护的公式块
        def _restore(m):
            return safe[int(m.group(1))]
        text = re.sub(r"⟦SAFE_(\d+)⟧", _restore, text)

        if dirty:
            _log.debug("sanitize_bare_latex: fixed bare LaTeX artifacts in LLM output")

        return text

    def _build_system(self, glossary: Optional[Glossary],
                      summary: Optional[str] = None,
                      multi_block: bool = False) -> str:
        """从 YAML 模板构建系统提示词。编号按有/无 summary+glossary 自动递增。"""
        prompts = _load_prompts()
        rule_num = len(prompts["rules"])
        parts = [prompts["intro"], *(prompts["rules"]), ""]
        if summary:
            rule_num += 1
            parts.append(prompts["summary_rule"].format(n=rule_num, summary=summary))
        gl_text = glossary.to_prompt_lines() if glossary else ""
        if gl_text:
            rule_num += 1
            parts.append(prompts["glossary_rule"].format(n=rule_num, glossary=gl_text))
        # 输出格式
        key = "output_multi" if multi_block else "output_single"
        parts.append(prompts[key].replace("{keep_as_is}", KEEP_AS_IS))
        return "\n\n".join(parts)

    @staticmethod
    def _normalize_author_marks(text: str) -> str:
        """后处理：把 arXiv 作者脚注的 LaTeX 标记替换为中文标注。

        ar5iv 将 <sup>*</sup> / <sup>🖂</sup> 包裹成行内公式
        ``^{*}$``（共同一作）与 ``^{🖂}``（通讯作者）。这些标记被 LLM
        原样保留进译文，影响可读性，故在翻译后统一替换为中文。

        同时处理紧贴作者姓名的写法（如 ``Xinzhe Huang^{*}$``）。
        """
        text = text.replace("^{*}", "（共同一作）")
        text = text.replace("^{🖂}", "（通讯作者）")
        # 个别论文可能用 ``^{\\text{*}}`` 等变体，兜底归一
        text = re.sub(r"\^\{\\?\\?text\{?\*?\}?\}", "（共同一作）", text)
        text = re.sub(r"\^\{\\?\\?text\{?🖂?\}?\}", "（通讯作者）", text)
        return text

    def translate(self, text: str, glossary: Optional[Glossary] = None,
                  summary: Optional[str] = None) -> tuple[str, list[dict[str, Any]]]:
        """翻译一段文本。

        返回 (译文, 本段确定的术语列表)。
        术语列表元素形如 {"en":..., "zh":..., "en_full"?:..., "note"?:...}。
        """
        if not text.strip():
            return text, []
        # 公式占位符保护：避免模型改写公式
        protected, formulas, references = self.protect_math(text)
        system = self._build_system(glossary, summary=summary)
        resp = self._client.chat.completions.create(
            model=self.llm.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": protected},
            ],
            temperature=self.llm.temperature,
            response_format={"type": "json_object"},
        )
        # 累计 token 用量（resp.usage 可能为 None，取决于提供方）
        if getattr(resp, "usage", None) is not None:
            self.usage.add(TokenUsage.from_response(resp.usage))
        raw = (resp.choices[0].message.content or "").strip()
        translation, terms = self._parse_json(raw)
        # 还原公式 + 后处理：修复 LLM 自行生成的裸 LaTeX 残骸
        translation = self.restore_math(translation, formulas, references)
        translation = self._sanitize_bare_latex(translation)
        # 作者脚注标记 → 中文
        translation = self._normalize_author_marks(translation)
        return translation, terms

    def translate_group(self, texts: list[str], glossary: Optional[Glossary] = None,
                        summary: Optional[str] = None) -> tuple[list[str], list[dict[str, Any]]]:
        """一次性翻译一组（合并的）短块。

        输入 texts 是若干个独立子块的原文列表。以 JSON 数组形式一次性发给模型，
        模型需对每个子块分别翻译并返回各自译文（而非直接拼接后整体译）。
        返回 (译文列表, 合并后的术语列表)，译文顺序与 texts 一一对应。
        """
        if not texts:
            return [], []
        # 单块退化为普通 translate，保持行为一致
        if len(texts) == 1:
            t, terms = self.translate(texts[0], glossary, summary)
            return [t], terms

        # 逐块公式保护
        protected_blocks: list[dict[str, Any]] = []
        formula_map: list[list[str]] = []  # 每个子块对应的占位符公式列表
        ref_map: list[list[str]] = []      # 每个子块对应的占位符引用列表
        for i, text in enumerate(texts):
            if not text.strip():
                protected_blocks.append({"id": i, "text": text})
                formula_map.append([])
                ref_map.append([])
                continue
            protected, formulas, references = self.protect_math(text)
            protected_blocks.append({"id": i, "text": protected})
            formula_map.append(formulas)
            ref_map.append(references)

        user_payload = json.dumps(
            {"blocks": protected_blocks},
            ensure_ascii=False,
        )
        system = self._build_system(glossary, summary=summary, multi_block=True)
        resp = self._client.chat.completions.create(
            model=self.llm.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_payload},
            ],
            temperature=self.llm.temperature,
            response_format={"type": "json_object"},
        )
        # 累计 token 用量（resp.usage 可能为 None，取决于提供方）
        if getattr(resp, "usage", None) is not None:
            self.usage.add(TokenUsage.from_response(resp.usage))
        raw = (resp.choices[0].message.content or "").strip()
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if not isinstance(parsed, dict) or "blocks" not in parsed:
            # 模型未按格式返回，逐块回退普通翻译
            fallback = []
            all_terms = []
            for text in texts:
                t, terms = self.translate(text, glossary, summary)
                fallback.append(t)
                all_terms.extend(terms)
            return fallback, all_terms

        # 还原：按 id 映射回译文
        translations: list[str] = [""] * len(texts)
        for item in parsed.get("blocks", []):
            try:
                idx = int(item["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= idx < len(texts):
                translations[idx] = self.restore_math(
                    str(item.get("translation", "")), formula_map[idx], ref_map[idx]
                )
        # 若模型漏掉某些块，用原文兜底并补翻
        for idx, t in enumerate(translations):
            if not t.strip():
                t0, _ = self.translate(texts[idx], glossary, summary)
                translations[idx] = t0
        # 后处理所有块：修复 LLM 自行生成的裸 LaTeX 残骸
        translations = [self._sanitize_bare_latex(t) for t in translations]
        # 作者脚注标记 → 中文
        translations = [self._normalize_author_marks(t) for t in translations]
        terms = parsed.get("terms", []) or []
        return translations, terms

    @staticmethod
    def _parse_json(raw: str) -> tuple[str, list[dict[str, Any]]]:
        """解析模型返回的 JSON，容错提取 translation 与 terms。"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # 容错：模型偶尔在 JSON 外包了 ```json 代码块
            # 使用非贪婪并按需平衡 {}（避免贪婪匹配跨过内部 {} 抓到非 JSON 区间）
            m = re.search(r"\{(?:[^{}]|\{[^{}]*\})*\}", raw, re.DOTALL)
            if not m:
                return raw, []
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return raw, []
        translation = data.get("translation", "") if isinstance(data, dict) else ""
        terms = data.get("terms", []) if isinstance(data, dict) else []
        if not isinstance(terms, list):
            terms = []
        return translation.strip(), terms
