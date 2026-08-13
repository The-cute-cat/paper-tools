"""DeepSeek 模型价目表动态获取与缓存。

价格数据**不写死**在代码里：运行时从官方定价页
https://api-docs.deepseek.com/zh-cn/quick_start/pricing 解析得到，并缓存到本地
JSON（默认 24h 有效期），以便离线/限流时回退到最近一次成功结果。

定价页为单一 <table>，价格部分以「价格(1)」为起始标记、三行价格（输入缓存命中 /
输入缓存未命中 / 输出），每个模型一列；模型名列（官方全名，如
DeepSeek-V4-Flash-0731）位于「模型版本」行。本模块把官方全名归一化为短名
（如 deepseek-v4-flash）作为查询键，并保留别名映射。
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

_log = logging.getLogger(__name__)

PRICING_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing"

# 缓存文件：放在本模块同目录，便于打包随包分发；首次运行时生成。
_CACHE_PATH = Path(__file__).resolve().parent / "pricing_cache.json"
_CACHE_TTL_SECONDS = 24 * 3600

# 价格行标签（用于从表格中定位三类价格）。以「包含」方式匹配，兼容中英文/细微差异。
_ROW_INPUT_HIT = "输入（缓存命中）"
_ROW_INPUT_MISS = "输入（缓存未命中）"
_ROW_OUTPUT = "百万tokens输出"
_PRICE_BLOCK_MARKER = "价格"


def _normalize_model_name(full: str) -> str:
    """把官方全名（DeepSeek-V4-Flash-0731）归一化为短名（deepseek-v4-flash）。

    规则：转小写 → 去版本号后缀（-MMDD / -YYYYMMDD）→ 'deepseek-' 前缀规范化。
    """
    s = full.strip().lower()
    # 去掉末尾的 -数字版本（如 -0731 / -0813 / -20240731）
    s = re.sub(r"-?\d{4,8}$", "", s)
    # 兼容 "deepseek-v4-flash" 已是短名的情况（无版本号则不变）
    if s.startswith("deepseek-"):
        return s
    # 处理 "deepseek-v4-flash-0731" → 上面已去版本号 → deepseek-v4-flash
    return s


def _parse_price_cell(cell: str) -> Optional[float]:
    """从 '0.02元' / '1元' / '2 元' / '0.5 USD' 等提取数值（人民币元）。

    仅支持人民币（元）；若页面出现其它币种则告警并返回 None。
    """
    if not cell:
        return None
    m = re.search(r"([\d.]+)\s*元", cell)
    if not m:
        if re.search(r"[A-Za-z]", cell) and "元" not in cell:
            _log.warning("定价页含非人民币币种，暂不支持：%r", cell)
        return None
    return float(m.group(1))


def _extract_pricing(soup: BeautifulSoup) -> dict[str, dict[str, float]]:
    """从解析后的页面提取 {短模型名: {cache_hit, cache_miss, output}}。

    output 字段为「每百万 token 人民币元」。

    解析策略不依赖行首标签的位置/列偏移（定价页用 rowspan 合并「价格(1)」
    标题单元格，导致首列数据错位）。改为：扫描每一行，若行文本含某价格类
    关键词（缓存命中 / 缓存未命中 / 输出），则在本行所有 cell 中提取「元」
    数值，按出现顺序对应各模型列。
    """
    table = soup.find("table")
    if table is None:
        raise ValueError("定价页未找到 <table>")

    rows = table.find_all("tr")
    model_short_names: list[str] = []
    # 三类价格：每个模型一列的数值列表（已解析为 float）
    price_cols: dict[str, list[float]] = {"cache_hit": [], "cache_miss": [], "output": []}

    for tr in rows:
        cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
        if not cells:
            continue
        row_text = " ".join(cells)
        # 模型版本行：定位各模型官方全名
        if cells[0] == "模型版本":
            model_short_names = [_normalize_model_name(n) for n in cells[1:] if n]
            continue
        # 价格行识别：行内出现「元」价格且含对应关键词
        kind = None
        if _ROW_INPUT_HIT in row_text:
            kind = "cache_hit"
        elif _ROW_INPUT_MISS in row_text:
            kind = "cache_miss"
        elif _ROW_OUTPUT in row_text and "输入" not in row_text:
            kind = "output"
        if kind is None:
            continue
        # 从本行所有 cell 抽取「元」数值（按出现顺序对应模型列）
        vals = [_parse_price_cell(c) for c in cells if "元" in c]
        vals = [v for v in vals if v is not None]
        if not vals:
            _log.warning("价格行未提取到数值，跳过：%r", cells)
            continue
        price_cols[kind] = vals

    if not model_short_names:
        raise ValueError("定价页未解析出模型版本行")
    # 列数校验：各价格列表长度应与模型列数一致
    for key, vals in price_cols.items():
        if vals and len(vals) != len(model_short_names):
            raise ValueError(
                f"价格列数({len(vals)})与模型列数({len(model_short_names)})不一致：{key}"
            )

    result: dict[str, dict[str, float]] = {}
    for i, short in enumerate(model_short_names):
        hit = price_cols["cache_hit"][i] if i < len(price_cols["cache_hit"]) else None
        miss = price_cols["cache_miss"][i] if i < len(price_cols["cache_miss"]) else None
        out = price_cols["output"][i] if i < len(price_cols["output"]) else None
        if hit is None or miss is None or out is None:
            _log.warning("模型 %s 价格解析不完整，跳过：hit=%s miss=%s out=%s",
                         short, hit, miss, out)
            continue
        result[short] = {"cache_hit": hit, "cache_miss": miss, "output": out}

    if not result:
        raise ValueError("未从定价页解析出任何有效价格")
    return result


def _load_cache() -> Optional[dict]:
    if not _CACHE_PATH.exists():
        return None
    try:
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        if time.time() - data.get("fetched_at", 0) > _CACHE_TTL_SECONDS:
            return None
        return data
    except Exception as exc:  # noqa: BLE001
        _log.warning("读取价目缓存失败：%s", exc)
        return None


def _save_cache(pricing: dict[str, dict[str, float]]) -> None:
    try:
        _CACHE_PATH.write_text(
            json.dumps(
                {"fetched_at": int(time.time()), "pricing": pricing},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("写入价目缓存失败：%s", exc)


def fetch_deepseek_pricing(force_refresh: bool = False) -> dict[str, dict[str, float]]:
    """获取 DeepSeek 价目表。

    优先读本地缓存（未过期）；force_refresh=True 或缓存缺失/失效时实时抓取并刷新缓存。
    抓取失败时若本地有任意缓存（即使过期）则回退，否则抛错。
    """
    if not force_refresh:
        cached = _load_cache()
        if cached is not None:
            return cached["pricing"]

    try:
        resp = requests.get(PRICING_URL, timeout=30)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        pricing = _extract_pricing(soup)
        _save_cache(pricing)
        return pricing
    except Exception as exc:  # noqa: BLE001
        _log.warning("实时抓取 DeepSeek 定价页失败：%s；尝试回退本地缓存", exc)
        cached = _load_cache()
        if cached is not None:
            _log.info("使用过期缓存价目表（fetched_at=%s）", cached.get("fetched_at"))
            return cached["pricing"]
        # 任何内嵌默认兜底都算写死，这里宁可失败让用户知情
        raise RuntimeError(
            "无法获取 DeepSeek 价目表（实时抓取与本地缓存均失败）。"
            "请检查网络或手动删除 pricing_cache.json 后重试。"
        ) from exc


def get_model_price(model: str) -> Optional[dict[str, float]]:
    """查询指定模型的价目（每百万 token 人民币元）。

    model 支持短名（deepseek-v4-flash）或官方全名（不区分大小写）。
    返回 {'cache_hit','cache_miss','output'} 或 None（未知模型）。
    """
    pricing = fetch_deepseek_pricing()
    key = model.strip().lower()
    if key in pricing:
        return pricing[key]
    # 尝试归一化匹配（兼容带版本号的全名）
    normalized = _normalize_model_name(model)
    if normalized in pricing:
        return pricing[normalized]
    # 前缀匹配：如 'deepseek-v4-flash-0731' 归属 'deepseek-v4-flash'
    for k, v in pricing.items():
        if k.startswith(normalized) or normalized.startswith(k):
            return v
    return None


if __name__ == "__main__":
    # 调试入口：打印当前解析到的价目表
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        p = fetch_deepseek_pricing(force_refresh=True)
        print(json.dumps(p, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
