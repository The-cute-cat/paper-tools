"""术语表（翻译记忆）：锁定易错/有歧义的术语，保证全文译法一致。

为什么需要术语表：
    学术论文里常有自创的方法/框架名（如 Furina）、上下文敏感的通用词
    （如 agent，通常保留或译“智能体”而非“代理”）。这些词若逐块翻译，
    容易前后不一致或被误译（如把框架名翻成“人名”）。术语表在首块确定
    译法后强制后续沿用，彻底避免该问题。

设计：
    - Term 记录：英文 key、中文译法（或“保留原文”标记）、英文全称（可选）、来源。
    - 翻译时把已有术语表喂给模型，要求严格沿用。
    - 每翻完一块，模型以结构化方式返回本块确定的关键术语，代码合并进表。
    - 可持久化为 JSON，方便复用与人工校对。
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# 模型在 structured 返回里用该标记表示“保留英文不翻译”
KEEP_AS_IS = "<KEEP>"

# 机器翻译高频误译的默认术语种子（中文学术规范译法，领域中立）。
# 在 seed 阶段之前注入，作为强制约束的兜底，避免模型把对齐/探针等翻错。
# 只保留跨领域通用、不因论文方向而变义的术语；领域专属词不写死在此处。
# 键为英文（小写归一），值为 (中文译法, 备注)。
DEFAULT_TERM_SEEDS: dict[str, tuple[str, str]] = {
    "alignment": ("对齐", "ML 领域指模型对齐，勿译“齐整/整齐”"),
    "artifacts": ("伪影", "CV/图像/信号中指非预期的结构或瑕疵，勿译“副产物”"),
    "probe": ("探针", "作为名词统一为“探针”（如线性探针），勿用动词“探测”"),
    "probes": ("探针", "probe 复数，统一为“探针”"),
    "split": ("数据集划分", "指数据集的 subset/split，译“划分”或“子集”，勿译“分割”(segmentation)"),
}

# 已知错误中文近义词 -> 正确译法。
# 用于返修阶段的“纯中文误译”检测：模型可能没沿用 glossary 的中文译法，
# 而是用了近义词（例如把“对齐”翻成“齐整”）。这些词不依赖英文单词边界，
# 用子串匹配捕获。只收录“术语性误译”（即几乎只会出现在术语语境的错词），
# 避免对普通中文造成误报。键为错误表达，值为应使用的正确译法。
WRONG_VARIANT_MAP: dict[str, str] = {
    "齐整机制": "对齐机制",
    "齐整训练": "对齐训练",
    "整齐机制": "对齐机制",
    "整齐训练": "对齐训练",
    "副产物": "伪影",
    "探测序列": "探针序列",
    "探测分析": "探针分析",
    "完整有害分割": "完整有害子集",
}



@dataclass
class Term:
    """一个术语条目。"""
    zh: str                          # 中文译法；若为 KEEP_AS_IS 表示保留英文原文
    en_full: Optional[str] = None    # 英文全称（缩写/简写类有，普通术语可无）
    note: Optional[str] = None       # 备注（如“论文自创框架名，勿译”）
    seen: bool = False               # 是否已在译文中出现过


@dataclass
class Glossary:
    """术语表：英文 key（小写归一） -> Term。"""

    terms: dict[str, Term] = field(default_factory=dict)

    # 匹配译文里的 `缩写（English Full, 中文）` 首现注解（兼容中/英逗号、全角括号）
    _FIRST_OCCUR_RE = re.compile(
        r"([A-Za-z][A-Za-z0-9\-/]{1,})"      # 缩写
        r"\（"                                # 全角左括号
        r"([^,，]+?)"                         # 英文全称
        r"[,，]\s*"                           # 逗号（中/英）
        r"([^）]+?)"                          # 中文
        r"\）"                                # 全角右括号
    )

    @classmethod
    def with_defaults(cls) -> "Glossary":
        """返回一个已注入领域默认术语种子的 Glossary。"""
        g = cls()
        for k, (zh, note) in DEFAULT_TERM_SEEDS.items():
            g.terms[k] = Term(zh=zh, note=note, seen=False)
        return g

    def add(self, key_en: str, zh: str, en_full: Optional[str] = None,
            note: Optional[str] = None) -> None:
        k = self._norm(key_en)
        if k not in self.terms:
            self.terms[k] = Term(zh=zh, en_full=en_full, note=note)
        else:
            t = self.terms[k]
            if not t.zh:
                t.zh = zh
            if en_full and not t.en_full:
                t.en_full = en_full
            if note and not t.note:
                t.note = note

    def get_zh(self, key_en: str) -> Optional[str]:
        t = self.terms.get(self._norm(key_en))
        return t.zh if t else None

    def has(self, key_en: str) -> bool:
        return self._norm(key_en) in self.terms

    def ingest_terms(self, terms: list[dict]) -> list[str]:
        """合并模型返回的结构化术语列表。

        terms 元素形如：
            {"en": "Furina", "zh": "<KEEP>", "note": "论文自创框架名"}
            {"en": "agent", "zh": "智能体"}
            {"en": "LLM", "zh": "大语言模型", "en_full": "Large Language Model"}
        返回新收录的英文 key（原样）。
        """
        added: list[str] = []
        for item in terms or []:
            en = (item.get("en") or "").strip()
            zh = (item.get("zh") or "").strip()
            if not en or not zh:
                continue
            k = self._norm(en)
            full = (item.get("en_full") or "").strip() or None
            note = (item.get("note") or "").strip() or None
            if k not in self.terms:
                self.terms[k] = Term(zh=zh, en_full=full, note=note, seen=True)
                added.append(en)
            else:
                t = self.terms[k]
                t.seen = True
                if t.zh == KEEP_AS_IS:
                    continue  # 已锁定保留原文，不被后续覆盖
                if zh != KEEP_AS_IS:
                    t.zh = zh
                if full and not t.en_full:
                    t.en_full = full
                if note and not t.note:
                    t.note = note
        return added

    def ingest_translation(self, translated_text: str) -> list[str]:
        """从译文里解析 `缩写（English Full, 中文）` 首现注解，作为补充收录。"""
        added: list[str] = []
        for abbr, en_full, zh in self._FIRST_OCCUR_RE.findall(translated_text):
            k = self._norm(abbr)
            if k not in self.terms:
                self.terms[k] = Term(zh=zh.strip(), en_full=en_full.strip(), seen=True)
                added.append(abbr)
            else:
                t = self.terms[k]
                t.seen = True
                if not t.en_full:
                    t.en_full = en_full.strip()
                if not t.zh:
                    t.zh = zh.strip()
        return added

    # 扫描英文原文里的“缩写 = 英文全称”定义（如 "H = Hate Speech"、
    # "MG: Malware Generation"、"F (Fraud)"）。提取为 缩写 -> 英文全称 关联，
    # 用于让模型在翻译含该缩写的句子（尤其图注/表头）时统一沿用全称译法，
    # 不依赖任何具体论文的硬编码术语。
    # 缩写允许单字母（如 H、F）或多字母/含点（如 MG、MIT、U.S.）。
    _ABBREV_DEF_RE = re.compile(
        r"(?<![A-Za-z0-9])"                                  # 左侧非字母数字（避免截断词）
        r"([A-Za-z][A-Za-z0-9.\-]{0,14})"                   # 缩写（首字母大写，1~15 字符）
        r"\s*[:=]\s*"                                        # = 或 :
        r"([A-Za-z][A-Za-z0-9 \-]*(?:[A-Za-z0-9]))"         # 英文全称短语
        r"(?=[,;.)\]\n]|\Z)"                                # 后接标点/结束
    )
    _ABBREV_PAREN_RE = re.compile(
        r"(?<![A-Za-z0-9])"
        r"([A-Za-z][A-Za-z0-9.\-]{0,14})\s*\(([A-Za-z][A-Za-z0-9 \-]+?)\)"  # ABBR (English)
    )

    def ingest_abbrev_defs(self, text_en: str) -> list[str]:
        """从英文原文里提取“缩写 = 英文全称”定义，存入术语表（仅记录英文全称）。

        返回新收录的缩写 key。这些条目暂不含中文译法，待对应图注/定义句翻译后
        由 ingest_translation 补全；但在翻译含该缩写的其它块时，术语表会提示模型
        “X 是 Y 的缩写”，从而与图注明文译法保持一致。
        """
        added: list[str] = []
        for m in self._ABBREV_DEF_RE.finditer(text_en or ""):
            abbr, en_full = m.group(1), m.group(2).strip()
            if len(en_full) <= len(abbr):  # 全称应明显长于缩写
                continue
            if abbr.lower() in self.terms:  # 已是普通术语，跳过
                continue
            k = self._norm(abbr)
            if k not in self.terms:
                self.terms[k] = Term(zh="", en_full=en_full, note=f"缩写，全称 {en_full}")
                added.append(abbr)
            else:
                t = self.terms[k]
                if not t.en_full:
                    t.en_full = en_full
                if not t.note:
                    t.note = f"缩写，全称 {en_full}"
        for m in self._ABBREV_PAREN_RE.finditer(text_en or ""):
            abbr, en_full = m.group(1), m.group(2).strip()
            if len(en_full) <= len(abbr):
                continue
            k = self._norm(abbr)
            if k not in self.terms:
                self.terms[k] = Term(zh="", en_full=en_full, note=f"缩写，全称 {en_full}")
                added.append(abbr)
        return added

    def to_prompt_lines(self, *, strict: bool = True) -> str:
        """生成喂给模型的术语约束文本。

        strict=True 时使用强指令格式（【强制术语约束】），并明确“违者以误译论处、
        必须覆盖默认翻译”，降低模型忽略术语表的概率。
        """
        if not self.terms:
            return ""
        lines = []
        for k, t in self.terms.items():
            if t.zh == KEEP_AS_IS:
                line = f"- {k} → 【保留英文原文，严禁翻译】"
            elif t.en_full and not t.zh:
                # 仅收录了缩写全称、尚未确定中文：提示模型沿用标准译法
                line = f"- {k}（{t.en_full}）→ 缩写，请采用其中文标准译法并与后文一致"
            elif t.en_full:
                line = f"- {k}（{t.en_full}）→ 【{t.zh}】"
            else:
                line = f"- {k} → 【{t.zh}】"
            if t.note:
                line += f"（{t.note}）"
            lines.append(line)
        body = "\n".join(lines)
        if not strict:
            return body
        return (
            "【强制术语约束 — 最高优先级】\n"
            "下文术语表是本文已锁定的标准译法，你必须严格沿用，逐字使用【】内的中文，"
            "不得换用近义词、不得直译、不得省略。若你的默认翻译与下表冲突，必须以本表为准。\n"
            f"{body}\n"
            "【强制术语约束结束】"
        )

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            k: {"zh": t.zh, "en_full": t.en_full, "note": t.note, "seen": t.seen}
            for k, t in self.terms.items()
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _norm(key_en: str) -> str:
        return key_en.strip().lower()
