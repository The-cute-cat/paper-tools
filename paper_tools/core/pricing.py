"""DeepSeek 模型价目表动态获取、解析与缓存。

价格数据**不写死**在代码里：运行时从官方定价页
https://api-docs.deepseek.com/zh-cn/quick_start/pricing 抓取并解析，缓存到本地
JSON（默认 24h 有效期），抓取或解析失败时回退到最近一次成功结果。

解析分两条通道，由 ``PAPER_TOOLS_PRICING_PARSER``（配置项 ``pricing_parser``）
选择：

* ``ai``（默认）——把页面正文整理后交给 LLM，让其按固定 JSON schema 抽取价格、
  模型名与别名。官方改版、模型改名/改价、峰谷档位增删都不需要改代码；脚注里的
  「旧模型名 X 仍可调用」也能一并抽出，从而把配置里的旧模型名映射到当前计费模型。
* ``rule``——本地规则解析（BeautifulSoup + 关键词/正则），零成本、结果确定，但
  官方一改列结构或文案措辞就可能失效（例如价目行标签由「输入（缓存命中）」改为
  「百万tokens输入 （缓存命中）」）。既是 ``ai`` 失败时的兜底，也可强制启用。

两条通道的产出都经过同一套 :func:`_validate_pricing` 校验（schema、数值范围、
峰谷单调性、模型名合法性）才写入缓存；缓存记录 ``source`` 标明来源。

峰谷档位：官方按「空闲（低谷）/ 高峰」两档定价，工作日 9:00-12:00、14:00-18:00
（北京时间）为高峰档，其余（含周末全天）为低谷档。
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import requests
import yaml
from bs4 import BeautifulSoup

_log = logging.getLogger(__name__)

PRICING_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing"

# 缓存文件：放在本模块同目录，便于打包随包分发；首次运行时生成。
_CACHE_PATH = Path(__file__).resolve().parent / "pricing_cache.json"
_CACHE_TTL_SECONDS = 24 * 3600

# LLM 抽取提示词模板（与 translator 一致：模板放 YAML，不硬编码在代码中）
_PROMPT_PATH = Path(__file__).resolve().parent / "pricing_prompts.yaml"
_CONTENT_PLACEHOLDER = "__PAGE_CONTENT__"
# 交给 LLM 的页面正文上限（字符）。正常定价页远小于该值，仅在页面异常膨胀时截断。
_MAX_CONTENT_CHARS = 16000

# ---------- 规则通道用的关键词（均已归一化：去空白、全角括号转半角） ----------
_ROW_INPUT_HIT = "输入(缓存命中)"
_ROW_INPUT_MISS = "输入(缓存未命中)"
_ROW_OUTPUT = "百万tokens输出"
_ROW_IDLE = "空闲时段"
_ROW_PEAK = "高峰时段"

# 三类价格列名
_COL_NAMES = ("cache_hit", "cache_miss", "output")
_TIERS = ("idle", "peak")

# 模型名合法字符：用于挡住把标签/说明语句误当成模型名的情况
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# 合法计价币种 / 单位（LLM 输出校验用）
_CURRENCY_OK = {"", "cny", "rmb", "cn¥", "¥", "￥", "人民币", "元"}
_UNIT_OK = {"", "per_million_tokens", "per_1m_tokens", "million_tokens", "per_million_token"}
# 每百万 token 人民币元的合理上限；超出几乎必然是解析错误
_MAX_PRICE = 1e5

# 价格单元格：数字 + 币种标记（如 "0.02元" / "1 元" / "¥4"）
_PRICE_RE = re.compile(r"([\d.]+)\s*(?:元|人民币|CNY|RMB|CN¥|¥|￥)", re.IGNORECASE)
# 版本号片段（v4 / 4.1 / 0731）：比较「模型家族」时忽略，见 _family_tokens
_VERSION_TOKEN_RE = re.compile(r"^v?\d+(?:\.\d+)*$")
# 非人民币币种提示（便于发现官方改用其它币种计价）
_FOREIGN_CURRENCY_RE = re.compile(r"[\d.]+\s*(?:美元|USD|\$|欧元|EUR|日元|JPY)", re.IGNORECASE)
# 单元格尾部的脚注角标，如 "deepseek-flash (1)"
_FOOTNOTE_MARK_RE = re.compile(r"\s*[（(]\s*\d+\s*[)）]\s*$")


def _settings():
    """延迟导入配置单例（避免核心模块与 config 之间形成导入环）。"""
    from ..config import get_settings

    return get_settings()


# ---------------------------------------------------------------- 文本归一化
def _norm_label(text: str) -> str:
    """归一化表格行/单元格文本，供「包含」匹配使用。

    去掉所有空白并把全角括号/冒号转为半角。官方文案常在同一标签里插入空格或换用
    全角符号（如「百万tokens输入 （缓存命中）」），直接字符串比较会漏匹配。
    """
    s = re.sub(r"\s+", "", text or "")
    for src, dst in (("（", "("), ("）", ")"), ("：", ":"), ("，", ",")):
        s = s.replace(src, dst)
    return s


def _clean_model_cell(text: str) -> str:
    """去掉单元格里的脚注角标与多余空白：'deepseek-flash (1)' → 'deepseek-flash'。"""
    return _FOOTNOTE_MARK_RE.sub("", (text or "").strip()).strip()


def _normalize_model_name(full: str) -> str:
    """把官方全名（DeepSeek-V4-Pro-0813）归一化为短名（deepseek-v4-pro）。

    规则：去脚注角标 → 转小写 → 去末尾版本号（-0731 / -20240731）。
    """
    s = _clean_model_cell(full).lower()
    s = re.sub(r"-?\d{4,8}$", "", s)
    return re.sub(r"-+$", "", s)


def _family_tokens(name: str) -> frozenset[str]:
    """提取模型名的「家族」词元，忽略版本号片段。

    ``deepseek-v4-flash`` / ``deepseek-flash`` / ``DeepSeek-V4.1-Flash`` 都得到
    ``{deepseek, flash}``，用于在精确名、别名、前缀匹配都失败时兜底归一化。
    """
    return frozenset(
        t for t in re.split(r"[-_.]", _clean_model_cell(name).lower())
        if t and not _VERSION_TOKEN_RE.match(t)
    )


def _parse_price_cell(cell: str) -> Optional[float]:
    """从 '0.02元' / '1元' / '¥4' 等单元格中提取人民币数值。

    仅支持人民币（元/¥/CNY...）；出现其它币种时告警并返回 None（宁可失败也不
    按错误币种估算）。
    """
    if not cell:
        return None
    m = _PRICE_RE.search(cell)
    if not m:
        if _FOREIGN_CURRENCY_RE.search(cell):
            _log.warning("定价页含非人民币币种，暂不支持：%r", cell)
        return None
    return float(m.group(1))


# ---------------------------------------------------------------- LLM 通道
@lru_cache(maxsize=1)
def _load_prompts() -> dict[str, str]:
    """加载 LLM 抽取提示词模板（模块级单例缓存）。"""
    with open(_PROMPT_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "system" not in data or "user" not in data:
        raise ValueError(f"提示词文件格式不正确（需含 system / user 键）：{_PROMPT_PATH}")
    return {"system": str(data["system"]), "user": str(data["user"])}


def _page_content_for_llm(html_text: str, max_chars: int = _MAX_CONTENT_CHARS) -> str:
    """把定价页整理成便于 LLM 抽取的精简文本。

    - 表格保留「行 → 单元格」结构（用 `` | `` 连接）：价格列与模型列的对应关系是
      正确解析的关键，纯文本展平会丢失该信息；
    - 表格之后附上正文（含脚注，用于识别模型别名）；
    - 剔除 script/style 等无关内容并压缩空白，控制 token 消耗。
    """
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "head"]):
        tag.decompose()

    lines: list[str] = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if any(cells):
                lines.append(" | ".join(cells))
        lines.append("")
    # 表格已单独提取，从正文里移除以免重复占 token
    for table in soup.find_all("table"):
        table.decompose()

    body = soup.get_text("\n", strip=True)
    text = "\n".join([*lines, body])
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    if len(text) > max_chars:
        _log.warning("定价页正文达 %d 字符，已截断至 %d 字符后交给 LLM 解析",
                     len(text), max_chars)
        text = text[:max_chars]
    return text


def _extract_pricing_ai(html_text: str) -> tuple[dict, dict]:
    """用 LLM 从定价页正文抽取价目表，返回 (pricing, aliases)。"""
    from openai import OpenAI

    llm = _settings().llm
    if not llm.api_key:
        raise RuntimeError("未配置 API key（DEEPSEEK_API_KEY），无法用 LLM 解析价目表")

    content = _page_content_for_llm(html_text)
    if not content.strip():
        raise ValueError("定价页正文为空，无法交给 LLM 解析")

    prompts = _load_prompts()
    user_prompt = prompts["user"].replace(_CONTENT_PLACEHOLDER, content)

    # 价目表是「估算别的调用花了多少钱」用的，本身不该成为长耗时环节：
    # 固定不重试并沿用配置超时，失败后由规则通道兜底。
    client = OpenAI(
        api_key=llm.api_key,
        base_url=llm.base_url,
        timeout=llm.timeout,
        max_retries=1,
    )
    try:
        resp = client.chat.completions.create(
            model=llm.model,
            messages=[
                {"role": "system", "content": prompts["system"]},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        raw = resp.choices[0].message.content or ""
    finally:
        client.close()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM 未返回合法 JSON：{raw[:200]!r}") from exc
    return _validate_pricing(data, content)


# ---------------------------------------------------------------- 规则通道
def _extract_pricing_rules(html_text: str) -> tuple[dict, dict]:
    """本地规则解析：BeautifulSoup + 关键词定位 + 正则抽数值，返回 (pricing, aliases)。

    不依赖行首标签的位置/列偏移（定价页用 rowspan/colspan 合并「价格」等标题单元格，
    导致首列数据错位）。改为：扫描每一行 → 按「空闲/高峰时段」关键词定档位 → 按行文本
    关键词（缓存命中/未命中/输出）识别类别 → 提取本行所有「元」数值，按出现顺序对应
    各模型列。

    模型名优先取表头「模型」行（官方在此给出**可实际调用**的 API 名，如
    ``deepseek-flash``），缺失时退回「模型版本」行并归一化。
    """
    soup = BeautifulSoup(html_text, "html.parser")
    table = soup.find("table")
    if table is None:
        raise ValueError("定价页未找到 <table>")

    api_names: list[str] = []
    version_names: list[str] = []
    # 各档位各类别：每个模型一列的数值列表（已解析为 float）
    tiers: dict[str, dict[str, list[float]]] = {
        "idle": {k: [] for k in _COL_NAMES},
        "peak": {k: [] for k in _COL_NAMES},
    }
    # 「高峰时段」行因 rowspan 吞掉了类别标签（只含「高峰时段」+ 数值），需继承其上
    # 「空闲时段」行识别出的类别。prev_kind 记录最近一次识别到的类别。
    prev_kind: Optional[str] = None

    for tr in table.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if not any(cells):
            continue
        first = _norm_label(cells[0])
        # 表头「模型」行：给出可实际调用的 API 模型名
        if first == "模型":
            api_names = [n for n in (_clean_model_cell(c) for c in cells[1:]) if n]
            continue
        # 「模型版本」行：给出官方全名（如 DeepSeek-V4-Pro-0813）
        if first == "模型版本":
            version_names = [n for n in (_clean_model_cell(c) for c in cells[1:]) if n]
            continue

        row_text = _norm_label(" ".join(cells))
        if _ROW_IDLE in row_text:
            tier = "idle"
        elif _ROW_PEAK in row_text:
            tier = "peak"
        else:
            # 非价格行（BASE URL / 上下文长度 / 并发限制等）
            continue

        # 类别识别：空闲行含类别关键词并记录；高峰行继承上一空闲行的类别
        kind = prev_kind
        if tier == "idle":
            if _ROW_INPUT_HIT in row_text:
                kind = "cache_hit"
            elif _ROW_INPUT_MISS in row_text:
                kind = "cache_miss"
            elif _ROW_OUTPUT in row_text:
                kind = "output"
            else:
                kind = None
            if kind is not None:
                prev_kind = kind
        if kind is None:
            continue

        # 从本行所有 cell 抽取人民币数值（按出现顺序对应模型列）
        vals = [v for v in (_parse_price_cell(c) for c in cells) if v is not None]
        if not vals:
            _log.warning("价格行未提取到数值，跳过：%r", cells)
            continue
        tiers[tier][kind] = vals

    names = api_names or version_names
    if not names:
        raise ValueError("定价页未解析出模型名（表头「模型」/「模型版本」行均缺失）")

    # 列数校验：各档位各价格列表长度应与模型列数一致
    for tier, cols in tiers.items():
        for key, vals in cols.items():
            if vals and len(vals) != len(names):
                raise ValueError(
                    f"{tier}/{key} 价格列数({len(vals)})与模型列数({len(names)})不一致"
                )

    pricing: dict[str, dict[str, dict[str, float]]] = {}
    for i, name in enumerate(names):
        short = name.lower()
        if not _NAME_RE.match(short):
            _log.warning("模型名非法，跳过：%r", name)
            continue
        entry: dict[str, dict[str, float]] = {}
        ok = True
        for tier in _TIERS:
            cols = tiers[tier]
            values = {col: (cols[col][i] if i < len(cols[col]) else None) for col in _COL_NAMES}
            if any(v is None for v in values.values()):
                _log.warning("模型 %s %s 档价格解析不完整，跳过：%s", short, tier, values)
                ok = False
                break
            entry[tier] = {col: float(values[col]) for col in _COL_NAMES}  # type: ignore[arg-type]
        if ok:
            pricing[short] = entry

    if not pricing:
        raise ValueError("未从定价页解析出任何有效价格")

    # 「模型版本」全名（如 DeepSeek-V4.1-Flash → deepseek-v4.1-flash）登记为别名，
    # 便于用户用版本名查询。官方更名的其它别名只能靠 LLM 通道从脚注里抽取。
    aliases: dict[str, str] = {}
    for i, full in enumerate(version_names):
        if i >= len(names):
            break
        key = names[i].lower()
        norm = _normalize_model_name(full)
        if key in pricing and norm and norm != key:
            aliases.setdefault(norm, key)
    return pricing, aliases


# ---------------------------------------------------------------- 结果校验
def _coerce_price(value: Any) -> Optional[float]:
    """把 LLM 返回的价格值转为 float，非法/超范围返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        price = float(value)
    elif isinstance(value, str):
        m = re.search(r"\d+(?:\.\d+)?", value)
        if m is None:
            return None
        price = float(m.group(0))
    else:
        return None
    if not 0.0 <= price < _MAX_PRICE:
        return None
    return price


def _norm_tier_name(name: Any) -> Optional[str]:
    """把 LLM 返回的档位名归一化为 idle / peak / default。"""
    s = str(name).strip().lower()
    if s in ("idle", "off_peak", "offpeak", "low"):
        return "idle"
    if s in ("peak", "high"):
        return "peak"
    if s in ("default", "single", "flat", "none", ""):
        return "default"
    return None


def _validate_pricing(data: Any, source_text: str = "") -> tuple[dict, dict]:
    """校验并归一化解析结果，返回 (pricing, aliases)。

    LLM 通道复用同一套规则通道的数据形状：``{模型名: {"idle"/"peak": {三类价格}}}``。
    校验不通过的部分直接丢弃（宁可缺价目也不给出错误费用）；若整体为空则抛错，交由
    上层回退缓存。校验项：

    * 币种必须为人民币、单位必须为每百万 token；
    * 模型名需为合法小写标识符，且（有正文时）应能在页面内容中找到，防臆造；
    * 三类价格均须存在且落在合理区间；
    * 空闲价应不高于高峰价（官方说明空闲价是高峰价的一半），否则视为峰谷颠倒并交换。
    """
    if not isinstance(data, dict):
        raise ValueError("解析结果不是 JSON 对象")

    currency = str(data.get("currency") or "").strip().lower()
    if currency not in _CURRENCY_OK:
        raise ValueError(f"不支持的计价币种：{currency!r}（仅支持人民币）")
    unit = str(data.get("unit") or "").strip().lower()
    if unit not in _UNIT_OK:
        raise ValueError(f"不支持的计价单位：{unit!r}（仅支持每百万 token）")

    models = data.get("models")
    if not isinstance(models, list) or not models:
        raise ValueError("解析结果缺少 models 列表")

    haystack = source_text.lower()
    pricing: dict[str, dict[str, dict[str, float]]] = {}
    aliases: dict[str, str] = {}

    for item in models:
        if not isinstance(item, dict):
            continue
        name = str(item.get("api_name") or "").strip().lower()
        if not _NAME_RE.match(name):
            _log.warning("跳过模型名非法的条目：%r", item.get("api_name"))
            continue

        tiers_raw = item.get("tiers")
        if not isinstance(tiers_raw, dict):
            _log.warning("模型 %s 缺少 tiers，跳过", name)
            continue

        parsed: dict[str, dict[str, float]] = {}
        for tier_name, tier_val in tiers_raw.items():
            tier = _norm_tier_name(tier_name)
            if tier is None or not isinstance(tier_val, dict):
                continue
            values: dict[str, float] = {}
            for col in _COL_NAMES:
                price = _coerce_price(tier_val.get(col))
                if price is None:
                    _log.warning("模型 %s %s.%s 价格非法：%r",
                                 name, tier_name, col, tier_val.get(col))
                    values = {}
                    break
                values[col] = price
            if values:
                parsed[tier] = values
        if not parsed:
            continue

        # 单档页面（无峰谷区分）→ 补齐为 idle/peak 两档，保持对外结构一致
        if "default" in parsed:
            parsed["idle"] = parsed["peak"] = parsed.pop("default")
        elif "peak" not in parsed:
            parsed["peak"] = parsed["idle"]
        elif "idle" not in parsed:
            parsed["idle"] = parsed["peak"]

        idle, peak = parsed["idle"], parsed["peak"]
        if any(idle[c] > peak[c] for c in _COL_NAMES):
            _log.warning("模型 %s 空闲价高于高峰价，疑似峰谷颠倒，已交换：idle=%s peak=%s",
                         name, idle, peak)
            parsed["idle"], parsed["peak"] = peak, idle

        if haystack and name not in haystack:
            _log.warning("模型名 %s 未出现在页面内容中，可能为臆造，请留意", name)

        pricing[name] = parsed
        for alias in item.get("aliases") or []:
            if not isinstance(alias, str):
                continue
            key = alias.strip().lower()
            if _NAME_RE.match(key) and key != name and key not in pricing:
                aliases.setdefault(key, name)

    if not pricing:
        raise ValueError("未从解析结果中得到任何有效模型价格")
    return pricing, aliases


# ---------------------------------------------------------------- 抓取与缓存
def _download_page() -> str:
    """下载定价页 HTML（带浏览器 UA，部分节点的 WAF 会拦截默认 UA）。"""
    resp = requests.get(
        PRICING_URL,
        timeout=30,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        },
    )
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def _parse_page(html_text: str) -> tuple[dict, dict, str]:
    """按配置的解析方式解析页面，返回 (pricing, aliases, source)。"""
    parser = (_settings().pricing_parser or "").strip().lower()
    if parser not in ("ai", "rule"):
        if parser:
            _log.warning("未知的价目表解析方式 %r，按 ai 处理", parser)
        parser = "ai"

    errors: list[str] = []
    # ai 模式失败时回落规则解析；rule 模式则只走规则
    for channel in (("ai", "rule") if parser == "ai" else ("rule",)):
        try:
            if channel == "ai":
                pricing, aliases = _extract_pricing_ai(html_text)
            else:
                pricing, aliases = _extract_pricing_rules(html_text)
        except Exception as exc:  # noqa: BLE001
            _log.warning("价目表 %s 解析失败：%s", channel, exc)
            errors.append(f"{channel}: {exc}")
            continue
        return pricing, aliases, channel
    raise RuntimeError("；".join(errors) or "无可用解析通道")


def _load_cache(allow_expired: bool = False) -> Optional[dict]:
    """读取价目缓存；allow_expired=True 时忽略 TTL（用于抓取失败后的兜底）。"""
    if not _CACHE_PATH.exists():
        return None
    try:
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        _log.warning("读取价目缓存失败：%s", exc)
        return None
    if not isinstance(data, dict) or "pricing" not in data:
        return None
    if not allow_expired and time.time() - data.get("fetched_at", 0) > _CACHE_TTL_SECONDS:
        return None
    return data


def _save_cache(pricing: dict, aliases: dict, source: str) -> None:
    try:
        _CACHE_PATH.write_text(
            json.dumps(
                {
                    "fetched_at": int(time.time()),
                    "source": source,
                    "pricing": pricing,
                    "aliases": aliases,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("写入价目缓存失败：%s", exc)


def _load_bundle(force_refresh: bool = False) -> tuple[dict, dict]:
    """获取 (pricing, aliases)。

    优先读未过期的本地缓存；force_refresh=True 或缓存缺失/失效时实时抓取并刷新。
    实时抓取失败时回退到本地缓存（**包括已过期的**），再失败才报错——任何内嵌默认
    兜底都算把价格写死，这里宁可失败让用户知情。
    """
    if not force_refresh:
        cached = _load_cache()
        if cached is not None:
            return cached.get("pricing") or {}, cached.get("aliases") or {}

    try:
        pricing, aliases, source = _parse_page(_download_page())
    except Exception as exc:  # noqa: BLE001
        _log.warning("实时获取 DeepSeek 价目表失败：%s；尝试回退本地缓存", exc)
        cached = _load_cache(allow_expired=True)
        if cached is not None:
            _log.info("改用本地缓存价目表（fetched_at=%s, source=%s；可能已过期）",
                      cached.get("fetched_at"), cached.get("source"))
            return cached.get("pricing") or {}, cached.get("aliases") or {}
        raise RuntimeError(
            "无法获取 DeepSeek 价目表（实时抓取与本地缓存均失败）。"
            "请检查网络或手动删除 pricing_cache.json 后重试。"
        ) from exc

    _save_cache(pricing, aliases, source)
    _log.info("已刷新 DeepSeek 价目表（解析方式=%s，模型=%s，别名=%s）",
              source, sorted(pricing), sorted(aliases))
    return pricing, aliases


def fetch_deepseek_pricing(force_refresh: bool = False) -> dict[str, dict[str, dict[str, float]]]:
    """获取 DeepSeek 价目表（仅价格部分）。

    返回结构 ``{模型名: {"idle": {...}, "peak": {...}}}``，价格为每百万 token 人民币元。
    """
    return _load_bundle(force_refresh)[0]


# ---------------------------------------------------------------- 峰谷与查询
_BJ_OFFSET = _dt.timezone(_dt.timedelta(hours=8))  # 北京时间 UTC+8，无夏令时


def _is_peak_hour(now: _dt.datetime) -> bool:
    """判断给定时刻（视为北京时间）是否为高峰时段。

    高峰时段为北京时间工作日（周一至周五）9:00-12:00、14:00-18:00；
    其余为空闲（低谷）时段。2026-08-23 起周末（周六、周日）全天不区分
    峰谷，统一按低谷价，因此周末一律视为非高峰。
    """
    if now.weekday() >= 5:  # 周六/周日
        return False
    h = now.hour + now.minute / 60.0 + now.second / 3600.0
    return (9 <= h < 12) or (14 <= h < 18)


def _resolve_tier(now: _dt.datetime | None = None) -> str:
    """返回当前应采用的档位：'peak' 或 'idle'（北京时间）。"""
    now = now or _dt.datetime.now(_BJ_OFFSET)
    return "peak" if _is_peak_hour(now) else "idle"


def get_model_price(model: str) -> Optional[dict[str, float]]:
    """查询指定模型的当前价目（每百万 token 人民币元）。

    model 支持可调用名（deepseek-flash）、官方全名或页面脚注列出的旧模型名别名，
    均不区分大小写；若都匹配不上，再按「家族」匹配（忽略版本号片段，把
    deepseek-v4-flash 归到 deepseek-flash），仅在唯一命中时采用。
    返回按当前北京时间自动选档后的 ``{'cache_hit','cache_miss','output'}``
    或 None（未知模型）。
    """
    pricing, aliases = _load_bundle()
    key = (model or "").strip().lower()
    norm = _normalize_model_name(key)

    entry: Optional[dict] = None
    for candidate in (key, aliases.get(key), norm, aliases.get(norm)):
        if candidate and candidate in pricing:
            entry = pricing[candidate]
            break
    if entry is None:
        # 前缀匹配：如 'deepseek-v4-flash-0731' 归属 'deepseek-v4-flash'
        entry = next((v for k, v in pricing.items()
                      if k.startswith(norm) or norm.startswith(k)), None)
    if entry is None:
        # 家族匹配（忽略版本号）：把 deepseek-v4-flash 归到 deepseek-flash。
        # 规则通道读不到脚注里的别名，靠这层兜住官方改名后的旧模型名；
        # 只在唯一命中时采用，歧义时宁可返回 None 也不猜。
        family = _family_tokens(key)
        if family:
            hits = [v for k, v in pricing.items() if _family_tokens(k) == family]
            if len(hits) == 1:
                entry = hits[0]
            elif len(hits) > 1:
                _log.warning("模型 %s 的家族匹配到多个候选，无法确定，跳过价目查询", model)
    if entry is None:
        return None

    # 新版结构：entry 含 idle/peak 两档，按当前时段选档
    if "idle" in entry and "peak" in entry:
        return entry[_resolve_tier()]
    # 单档页面 / 旧版缓存：entry 直接是标量 dict，原样返回
    return entry.get("default", entry)


if __name__ == "__main__":
    # 调试入口：打印当前解析到的价目表、别名与按当前时段的选档结果
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        _pricing, _aliases = _load_bundle(force_refresh=True)
        print(json.dumps({"pricing": _pricing, "aliases": _aliases},
                         ensure_ascii=False, indent=2))
        _tier = _resolve_tier()
        print(f"\n当前档位: {_tier} ({_dt.datetime.now(_BJ_OFFSET):%Y-%m-%d %H:%M %Z})")
        for _name in _pricing:
            print(f"  {_name}: {_pricing[_name].get(_tier)}")
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
